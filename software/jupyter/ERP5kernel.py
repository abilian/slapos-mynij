from ipykernel.kernelbase import Kernel
from ipykernel.kernelapp import IPKernelApp
from IPython.core.display import HTML
import requests
import json
import sys

erp5_url = None
if len(sys.argv) > 1:
    erp5_url = "%s/erp5/Base_executeJupyter" % (sys.argv[1],)

class MagicInfo:
  """
  Magics definition structure.
  Initializes a new MagicInfo class with specific paramters to identify a magic.
  """
  def __init__(self, magic_name, variable_name, send_request, request_reference, display_message):
    self.magic_name = magic_name
    self.variable_name = variable_name
    self.send_request = send_request
    self.request_reference = request_reference
    self.display_message = display_message

# XXX: New magics to be added here in the dictionary.
# In this dictionary,
# key = magic_name,
# value = MagicInfo Structure corresponding to the magics
# Different parameters of the structures are :-
# magics_name(str) = Name which would be used on jupyter frontend
# variable_name(str) = Name of variable on which magic would be set in kernel
# send_request(boolean) = Magics for which requests to erp5 backend need to be made
# request_reference(boolean) = Request for notebook references(and titles) from erp5
# display_message(boolean) = If the magics need to display message after
#                             making request. Useful for magics which do get some
#                             useful content from erp5 backend and need to display

MAGICS = {
  'erp5_user': MagicInfo('erp5_user', 'user', True, False, True),
  'erp5_password': MagicInfo('erp5_password', 'password', True, False, True),
  'erp5_url': MagicInfo('erp5_url', 'url', True, False, True),
  'notebook_set_reference': MagicInfo('notebook_set_reference', 'reference', True, False, True),
  'notebook_set_title': MagicInfo('notebook_set_title', 'title', False, False, True),
  'my_notebooks': MagicInfo('my_notebooks', '', True, True, False)}

class ERP5Kernel(Kernel):
  """
  Jupyter Kernel class to interact with erp5 backend for code from frontend.
  To use this kernel with erp5, user need to install 'erp5_data_notebook' bt5 
  Also, handlers(aka magics) starting with '%' are predefined.

  Each request to erp5 for code execution requires erp5_user, erp5_password
  and reference of the notebook.
  """

  implementation = 'ERP5'
  implementation_version = '1.0'
  language = 'ERP5'
  language_version = '0.1'
  language_info = {'mimetype': 'text/plain', 'name':'python'}
  banner = "ERP5 integration with jupyter notebook"

  def __init__(self, user=None, password=None, url=None, status_code=None,
              *args, **kwargs):
    super(ERP5Kernel, self).__init__(*args, **kwargs)
    self.user = user
    self.password = password
    # By default use URL provided by buildout during initiation
    # It can later be overridden
    if url is None:
        self.url = erp5_url
    else:
        self.url = url
    self.status_code = status_code
    self.reference = None
    self.title = None
    # Allowed HTTP request code list for making request to erp5 from Kernel
    # This list should be to used check status_code before making requests to erp5
    self.allowed_HTTP_request_code_list = list(range(500, 511))
    # Append request code 200 in the allowed HTTP status code list
    self.allowed_HTTP_request_code_list.append(200)

  def display_response(self, response=None):
    """
      Dispays the stream message response to jupyter frontend.
    """
    if response:
      stream_content = {'name': 'stdout', 'text': response}
      self.send_response(self.iopub_socket, 'stream', stream_content)

  def set_magic_attribute(self, magic_info=None, code=None):
    """
      Set attribute for magic which are necessary for making requests to erp5.
      Catch errors and display message. Since user is in contact with jupyter
      frontend, so its better to catch exceptions and dispaly messages than to
      let them fail in backend and stuck the kernel.
      For a making a request to erp5, we need -
      erp5_url, erp5_user, erp5_password, notebook_set_reference
    """
    # Set attributes only for magic who do have any varible to set value to
    if magic_info.variable_name:
      try:
        # Get the magic value recived via code from frontend
        magic_value = code.split()[1]
        # Set magic_value to the required attribute
        
        if magic_info.magic_name == 'notebook_set_reference':
          required_attributes = ['url', 'password', 'user']
          missing_attributes = []
          for attribute in required_attributes:
            if not getattr(self, attribute):
              missing_attributes.append(attribute)
          
          if missing_attributes != []:
            self.response = "You still haven't entered all required magics. \
Please do so before inputting your reference."
          else:
            if self.check_existing_reference(reference=magic_value):
              self.response = 'WARNING: You already have a notebook with \
reference %s. It might be a good idea to use different references for new \
notebooks. \n' % magic_value
            else:
              self.response = ''
            setattr(self, magic_info.variable_name , magic_value)
            self.response = self.response + 'Your %s is %s. '%(magic_info.magic_name, magic_value)
        elif magic_info.magic_name != 'erp5_password':
          setattr(self, magic_info.variable_name , magic_value)
          self.response = 'Your %s is %s. '%(magic_info.magic_name, magic_value)
        else:
          setattr(self, magic_info.variable_name , magic_value)
          self.response = ""

      # Catch exception while setting attribute and set message in response
      except AttributeError:
        self.response = 'Please enter %s magic value'%magic_info.variable_name

      # Catch IndexError while getting magic_value and set message in response object
      except IndexError:
        self.response = 'Empty value for %s magic'%magic_info.variable_name

      # Catch all other exceptions and set error_message in response object
      # XXX: Might not be best way, but its better to display error to the user
      # via notebook frontend than to fail in backend and stuck the Kernel without
      # any failure message to user.
      except Exception as e:
        self.response = str(e)

      # Display the message/response from this fucntion before moving forward so
      # as to keep track of the status
      if self.response != "":
        self.display_response(response=(self.response + '\n'))

  def check_required_attributes(self):
    """
      Check if the required attributes for making a request are already set or not.
      Display message to frontend to provide with the values in case they aren't.
      This function can be called anytime to check if the attributes are set. The
      output result will be in Boolean form.
      Also, in case any of attribute is not set, call to display_response would be
      made to ask user to enter value.
    """
    result_list = []
    required_attributes  = ['url', 'user', 'password', 'reference']
    missing_attributes = []

    # Set response to empty so as to flush the response set by some earlier fucntion call
    self.response = ''

    # Loop to check if the required attributes are set
    for attribute in required_attributes:
      if getattr(self, attribute):
        result_list.append(True)
      else:
        # Set response/message for attributes which aren't set
        missing_attributes.append(attribute)
        result_list.append(False)
    
    # Compare result_list to get True for all True results and False for any False result 
    check_attributes = all(result_list)
    
    if check_attributes:
      self.response = 'You have entered all required magics. You may now use your notebook.'
    else:
      self.response = '''You have these required magics remaining: %s. \n''' % (
      ', '.join(map(str, missing_attributes)))

    # Display response to frontend before moving forward
    self.display_response(response=(self.response + '\n'))

    return check_attributes

  def make_erp5_request(self, request_reference=False, display_message=True,
                        code=None, message=None, title=None, *args, **kwargs):
    """
      Function to make request to erp5 as per the magics.
      Should return the response json object.
    """

    try:
      erp5_request = requests.post(
        self.url,
        verify=False,
        auth=(self.user, self.password),
        data={
          'python_expression': code,
          'reference': self.reference,
          'title': self.title,
          'request_reference': request_reference,
	  'store_history': kwargs.get('store_history')
          })

      # Set value for status_code for self object which would later be used to
      # dispaly response after statement check
      self.status_code = erp5_request.status_code

      # Dispaly error response in case the request give any other status
      # except 200 and 5xx(which is for errors on server side)
      if self.status_code not in self.allowed_HTTP_request_code_list:
        self.response = '''Error code %s on request to ERP5,\n
        check credentials or ERP5 family URL'''%self.status_code
      else:
        # Set value of self.response to the given value in case response from function
        # call. In all other case, response should be the content from request
        if display_message and message:
          self.response = message
        else:
          self.response = erp5_request.content

    except requests.exceptions.RequestException as e:
      self.response = str(e)

  def do_execute(self, code, silent, store_history=True, user_expressions=None,
                  allow_stdin=False):
    """
      Validate magic and call functions to make request to erp5 backend where
      the code is being executed and response is sent back which is then sent
      to jupyter frontend.
    """
    # By default, take the status of response as 'ok' so as show the responses
    # for erp5_url and erp5_user on notebook frontend as successful response.
    status = 'ok'

    if not silent:
      # Remove spaces and newlines from both ends of code
      code = code.strip()

      extra_data_list = []
      print_result = {}
      displayhook_result = {}

      if code.startswith('%'):
          # No need to try-catch here as its already been taken that the code
          # starts-with '%', so we'll get magic_name, no matter what be after '%'
          magic_name = code.split()[0][1:]
          magics_name_list = [magic.magic_name for magic in MAGICS.values()]

          # Check validation of magic
          if magic_name and magic_name in magics_name_list:

            # Get MagicInfo object related to the magic
            magic_info = MAGICS.get(magic_name)

            # Function call to set the required magics
            self.set_magic_attribute(magic_info=magic_info, code=code)

            # Call to check if the required_attributes are set
            checked_attribute = self.check_required_attributes()
            if checked_attribute and magic_info.send_request:
              # Call the function to send request to erp5 with the arguments given
              self.make_erp5_request(message='Please proceed\n',
              request_reference=magic_info.request_reference,
              display_message=magic_info.display_message)

              # Display response from erp5 request for magic
              # Since this response would be either success message or failure
              # error message, both of which are string type, so, we can simply
              # display the stream response.
              if self.response != 'Please proceed\n':
                self.display_response(response=self.response)

          else:
            # Set response if there is no magic or the magic name is not in MAGICS
            self.response = 'Invalid Magics'
            self.display_response(response=self.response)

      else:
        # Check for status_code before making request to erp5 and make request in
        # only if the status_code is in the allowed_HTTP_request_code_list
        if self.status_code in self.allowed_HTTP_request_code_list:
          self.make_erp5_request(code=code, store_history=store_history)

          # For 200 status_code, Kernel will receive predefined format for data
          # from erp5 which is either json of result or simple result string
          if self.status_code == 200:
            mime_type = 'text/plain'
            try:
              content = json.loads(self.response)

              # Example format for the json result we are expecting is :
              # content = {
              #            "status": "ok",
              #            "ename": null,
              #            "evalue": null,
              #            "traceback": null,
              #            "code_result": "",
              #            "print_result": {},
              #            "displayhook_result": {},
              #            "mime_type": "text/plain",
              #            "extra_data_list": []
              #            }
              # So, we can easily use any of the key to update values as such.

              # Getting code_result for succesfull execution of code
              code_result = content['code_result']
              print_result = content['print_result']
              displayhook_result = content['displayhook_result']

              # Update mime_type with the mime_type from the http response result
              # Required in case the mime_type is anything other than 'text/plain'
              mime_type = content['mime_type']

              extra_data_list = content.get('extra_data_list', [])

            # Display to frontend the error message for content status as 'error'
              if content['status']=='error':
                reply_content = {
                  'status': 'error',
                  'execution_count': self.execution_count,
                  'ename': content['ename'],
                  'evalue': content['evalue'],
                  'traceback': content['traceback']}
                self.send_response(self.iopub_socket, u'error', reply_content)
                return reply_content
            # Catch exception for content which isn't json
            except ValueError:
              content = self.response
              code_result = content
              print_result = {'data':{'text/plain':content}, 'metadata':{}}
          # Display basic error message to frontend in case of error on server side
          else:
            self.make_erp5_request(code=code)
            code_result = "Error at Server Side"
            print_result = {'data':{'text/plain':'Error at Server Side'}, 'metadata':{}}
            mime_type = 'text/plain'

        # For all status_code except allowed_HTTP_response_code_list show unauthorized message
        else:
          code_result = 'Unauthorized access'
          print_result = {'data':{'text/plain':'Unauthorized access'}, 'metadata':{}}
          mime_type = 'text/plain'

        if print_result.get('data'):
          self.send_response(self.iopub_socket, 'display_data', print_result)

        if displayhook_result.get('data'):
          displayhook_result['execution_count'] = self.execution_count
          self.send_response(self.iopub_socket, 'execute_result', displayhook_result)

        for extra_data in extra_data_list:
          self.send_response(self.iopub_socket, 'display_data', extra_data)

    reply_content = {
      'status': status,
      # The base class increments the execution count
      'execution_count': self.execution_count,
      'payload': [],
      'user_expressions': {}}

    return reply_content
  
  # Checks the ERP5 site if there are existing notebooks with the same reference.
  # Returns True if there are.
  def check_existing_reference(self, reference):
    if reference == None:
      return False
    
    modified_url = self.url[:self.url.rfind('/')] + '/Base_checkExistingReference'
    result = True
    
    try:
      erp5_request = requests.post(
        modified_url,
        verify=False,
        auth=(self.user, self.password),
        data={
          'reference': reference,
          })
      result = erp5_request.content
      
    except requests.exceptions.RequestException as e:
      self.response = str(e)
      
    return result


if __name__ == '__main__':
  IPKernelApp.launch_instance(kernel_class=ERP5Kernel)
