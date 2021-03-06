##############################################################################
#
# Copyright (c) 2018 Nexedi SA and Contributors. All Rights Reserved.
#
# WARNING: This program as such is intended to be used by professional
# programmers who take the whole responsibility of assessing all potential
# consequences resulting from its eventual inadequacies and bugs
# End users who are looking for a ready-to-use solution with commercial
# guarantees and support are strongly advised to contract a Free Software
# Service Company
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
##############################################################################

import glob
import os
import requests
import httplib
from requests_toolbelt.adapters import source
import json
import multiprocessing
import subprocess
from unittest import skip
import ssl
from BaseHTTPServer import HTTPServer
from BaseHTTPServer import BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn
import time
import tempfile
import ipaddress
import StringIO
import gzip
import base64
import re
from slapos.recipe.librecipe import generateHashFromFiles
import xml.etree.ElementTree as ET
import urlparse
import socket
import sys
import logging
import random
import string


try:
    import lzma
except ImportError:
    from backports import lzma

import datetime

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from slapos.testing.testcase import makeModuleSetUpAndTestCaseClass
from slapos.testing.utils import findFreeTCPPort
from slapos.testing.utils import getPromisePluginParameterDict
if int(os.environ.get('SLAPOS_HACK_STANDALONE', '0')) == 1:
  SlapOSInstanceTestCase = object
else:
  setUpModule, SlapOSInstanceTestCase = makeModuleSetUpAndTestCaseClass(
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'software.cfg')))


# ports chosen to not collide with test systems
HTTP_PORT = '11080'
HTTPS_PORT = '11443'
CAUCASE_PORT = '15090'
KEDIFA_PORT = '15080'

# IP to originate requests from
# has to be not partition one
SOURCE_IP = '127.0.0.1'

# IP on which test run, in order to mimic HTTP[s] access
TEST_IP = os.environ['SLAPOS_TEST_IPV4']

# "--resolve" inspired from https://stackoverflow.com/a/44378047/9256748
DNS_CACHE = {}


def add_custom_dns(domain, port, ip):
  port = int(port)
  key = (domain, port)
  value = (socket.AF_INET, 1, 6, '', (ip, port))
  DNS_CACHE[key] = [value]


def new_getaddrinfo(*args):
  return DNS_CACHE[args[:2]]


# for development: debugging logs and install Ctrl+C handler
if os.environ.get('SLAPOS_TEST_DEBUG'):
  logging.basicConfig(level=logging.DEBUG)
  import unittest
  unittest.installHandler()


def der2pem(der):
  certificate = x509.load_der_x509_certificate(der, default_backend())
  return certificate.public_bytes(serialization.Encoding.PEM)


# comes from https://stackoverflow.com/a/21788372/9256748
def patch_broken_pipe_error():
    """Monkey Patch BaseServer.handle_error to not write
    a stacktrace to stderr on broken pipe.
    https://stackoverflow.com/a/7913160"""
    from SocketServer import BaseServer

    handle_error = BaseServer.handle_error

    def my_handle_error(self, request, client_address):
        type, err, tb = sys.exc_info()
        # there might be better ways to detect the specific erro
        if repr(err) == "error(32, 'Broken pipe')":
            pass
        else:
            handle_error(self, request, client_address)

    BaseServer.handle_error = my_handle_error


patch_broken_pipe_error()


def createKey():
  key = rsa.generate_private_key(
    public_exponent=65537, key_size=2048, backend=default_backend())
  key_pem = key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption()
  )
  return key, key_pem


def createSelfSignedCertificate(name_list):
  key, key_pem = createKey()
  subject_alternative_name_list = x509.SubjectAlternativeName(
    [x509.DNSName(unicode(q)) for q in name_list]
  )
  subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, u'Test Self Signed Certificate'),
  ])
  certificate = x509.CertificateBuilder().subject_name(
    subject
  ).issuer_name(
    issuer
  ).add_extension(
      subject_alternative_name_list,
      critical=False,
  ).public_key(
    key.public_key()
  ).serial_number(
    x509.random_serial_number()
  ).not_valid_before(
    datetime.datetime.utcnow() - datetime.timedelta(days=2)
  ).not_valid_after(
    datetime.datetime.utcnow() + datetime.timedelta(days=5)
  ).sign(key, hashes.SHA256(), default_backend())
  certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
  return key, key_pem, certificate, certificate_pem


def createCSR(common_name, ip=None):
  key, key_pem = createKey()
  subject_alternative_name_list = []
  if ip is not None:
    subject_alternative_name_list.append(
      x509.IPAddress(ipaddress.ip_address(unicode(ip)))
    )
  csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
     x509.NameAttribute(NameOID.COMMON_NAME, unicode(common_name)),
  ]))

  if len(subject_alternative_name_list):
    csr = csr.add_extension(
      x509.SubjectAlternativeName(subject_alternative_name_list),
      critical=False
    )

  csr = csr.sign(key, hashes.SHA256(), default_backend())
  csr_pem = csr.public_bytes(serialization.Encoding.PEM)
  return key, key_pem, csr, csr_pem


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
  pass


class CertificateAuthority(object):
  def __init__(self, common_name):
    self.key, self.key_pem = createKey()
    public_key = self.key.public_key()
    builder = x509.CertificateBuilder()
    builder = builder.subject_name(x509.Name([
      x509.NameAttribute(NameOID.COMMON_NAME, unicode(common_name)),
    ]))
    builder = builder.issuer_name(x509.Name([
      x509.NameAttribute(NameOID.COMMON_NAME, unicode(common_name)),
    ]))
    builder = builder.not_valid_before(
      datetime.datetime.utcnow() - datetime.timedelta(days=2))
    builder = builder.not_valid_after(
      datetime.datetime.utcnow() + datetime.timedelta(days=30))
    builder = builder.serial_number(x509.random_serial_number())
    builder = builder.public_key(public_key)
    builder = builder.add_extension(
      x509.BasicConstraints(ca=True, path_length=None), critical=True,
    )
    self.certificate = builder.sign(
      private_key=self.key, algorithm=hashes.SHA256(),
      backend=default_backend()
    )
    self.certificate_pem = self.certificate.public_bytes(
      serialization.Encoding.PEM)

  def signCSR(self, csr):
    builder = x509.CertificateBuilder(
      subject_name=csr.subject,
      extensions=csr.extensions,
      issuer_name=self.certificate.subject,
      not_valid_before=datetime.datetime.utcnow() - datetime.timedelta(days=1),
      not_valid_after=datetime.datetime.utcnow() + datetime.timedelta(days=30),
      serial_number=x509.random_serial_number(),
      public_key=csr.public_key(),
    )
    certificate = builder.sign(
      private_key=self.key,
      algorithm=hashes.SHA256(),
      backend=default_backend()
    )
    return certificate, certificate.public_bytes(serialization.Encoding.PEM)


def subprocess_status_output(*args, **kwargs):
  prc = subprocess.Popen(
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    *args,
    **kwargs)
  out, err = prc.communicate()
  return prc.returncode, out


def subprocess_output(*args, **kwargs):
  return subprocess_status_output(*args, **kwargs)[1]


def isHTTP2(domain):
  curl_command = 'curl --http2 -v -k -H "Host: %(domain)s" ' \
    'https://%(domain)s:%(https_port)s/ '\
    '--resolve %(domain)s:%(https_port)s:%(ip)s' % dict(
      ip=TEST_IP, domain=domain, https_port=HTTPS_PORT)
  prc = subprocess.Popen(
    curl_command.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
  )
  out, err = prc.communicate()
  assert prc.returncode == 0, "Problem running %r. Output:\n%s\nError:\n%s" % (
    curl_command, out, err)
  return 'Using HTTP2, server supports multi-use' in err


class TestDataMixin(object):
  def getTrimmedProcessInfo(self):
    return '\n'.join(sorted([
      '%(group)s:%(name)s %(statename)s' % q for q
      in self.callSupervisorMethod('getAllProcessInfo')
      if q['name'] != 'watchdog' and q['group'] != 'watchdog']))

  def assertTestData(self, runtime_data, hash_value_dict=None, msg=None):
    if hash_value_dict is None:
      hash_value_dict = {}
    filename = '%s-%s.txt' % (self.id(), 'CADDY')
    test_data_file = os.path.join(
      os.path.dirname(os.path.realpath(__file__)), 'test_data', filename)

    try:
      test_data = open(test_data_file).read().strip()
    except IOError:
      test_data = ''

    for hash_type, hash_value in hash_value_dict.items():
      runtime_data = runtime_data.replace(hash_value, '{hash-%s}' % (
        hash_type),)

    maxDiff = self.maxDiff
    self.maxDiff = None
    longMessage = self.longMessage
    self.longMessage = True
    try:
      self.assertMultiLineEqual(
        test_data,
        runtime_data,
        msg=msg
      )
    except AssertionError:
      if os.environ.get('SAVE_TEST_DATA', '0') == '1':
        open(test_data_file, 'w').write(runtime_data.strip() + '\n')
      raise
    finally:
      self.maxDiff = maxDiff
      self.longMessage = longMessage

  def _test_file_list(self, slave_dir_list, IGNORE_PATH_LIST=None):
    if IGNORE_PATH_LIST is None:
      IGNORE_PATH_LIST = []
    runtime_data = []
    for slave_var in glob.glob(os.path.join(self.instance_path, '*')):
      for entry in os.walk(os.path.join(slave_var, *slave_dir_list)):
        for filename in entry[2]:
          path = os.path.join(
            entry[0][len(self.instance_path) + 1:], filename)
          if not any([path.endswith(q) for q in IGNORE_PATH_LIST]):
            runtime_data.append(path)
    runtime_data = '\n'.join(sorted(runtime_data))
    self.assertTestData(runtime_data)

  def test_file_list_log(self):
    self._test_file_list(['var', 'log'], [
      # no control at all when cron would kick in, ignore it
      'cron.log',
      # appears late and is quite unstable, no need to assert
      'trafficserver/.diags.log.meta',
      'trafficserver/.manager.log.meta',
      'trafficserver/.squid.log.meta',
      'trafficserver/diags.log',
      'trafficserver/squid.log',
      # not important, appears sometimes
      'trafficserver/.error.log.meta',
      'trafficserver/error.log',
      'trafficserver/.traffic.out.meta',
      'trafficserver/traffic.out',
    ])

  def test_file_list_run(self):
    self._test_file_list(['var', 'run'], [
      # can't be sure regarding its presence
      'caddy_configuration_last_state',
      'validate_configuration_state_signature',
      # run by cron from time to time
      'monitor/monitor-collect.pid',
      # no control regarding if it would or not be running
      'monitor/monitor-bootstrap.pid',
    ])

  def test_file_list_etc_cron_d(self):
    self._test_file_list(['etc', 'cron.d'])

  def test_file_list_plugin(self):
    self._test_file_list(['etc', 'plugin'], ['.pyc'])

  def test_supervisor_state(self):
    # give a chance for etc/run scripts to finish
    time.sleep(1)

    hash_file_list = [os.path.join(
        self.computer_partition_root_path, 'software_release/buildout.cfg')]
    hash_value_dict = {
      'generic': generateHashFromFiles(hash_file_list),
    }
    for caddy_wrapper_path in glob.glob(os.path.join(
      self.instance_path, '*', 'bin', 'caddy-wrapper')):
      partition_id = caddy_wrapper_path.split('/')[-3]
      hash_value_dict[
        'caddy-%s' % (partition_id)] = generateHashFromFiles(
        [caddy_wrapper_path] + hash_file_list
      )
    for backend_haproxy_wrapper_path in glob.glob(os.path.join(
      self.instance_path, '*', 'bin', 'backend-haproxy-wrapper')):
      partition_id = backend_haproxy_wrapper_path.split('/')[-3]
      hash_value_dict[
        'backend-haproxy-%s' % (partition_id)] = generateHashFromFiles(
        [backend_haproxy_wrapper_path] + hash_file_list
      )
    for rejected_slave_publish_path in glob.glob(os.path.join(
      self.instance_path, '*', 'etc', 'nginx-rejected-slave.conf')):
      partition_id = rejected_slave_publish_path.split('/')[-3]
      rejected_slave_pem_path = os.path.join(
        self.instance_path, partition_id, 'etc', 'rejected-slave.pem')
      hash_value_dict[
        'rejected-slave-publish'
      ] = generateHashFromFiles(
        [rejected_slave_publish_path, rejected_slave_pem_path] + hash_file_list
      )

    runtime_data = self.getTrimmedProcessInfo()
    self.assertTestData(runtime_data, hash_value_dict=hash_value_dict)


def fakeHTTPSResult(domain, path, port=HTTPS_PORT,
                    headers=None, cookies=None, source_ip=SOURCE_IP):
  if headers is None:
    headers = {}
  # workaround request problem of setting Accept-Encoding
  # https://github.com/requests/requests/issues/2234
  headers.setdefault('Accept-Encoding', 'dummy')
  # Headers to tricks the whole system, like rouge user would do
  headers.setdefault('X-Forwarded-For', '192.168.0.1')
  headers.setdefault('X-Forwarded-Proto', 'irc')
  headers.setdefault('X-Forwarded-Port', '17')

  session = requests.Session()
  if source_ip is not None:
    new_source = source.SourceAddressAdapter(source_ip)
    session.mount('http://', new_source)
    session.mount('https://', new_source)
  socket_getaddrinfo = socket.getaddrinfo
  try:
    add_custom_dns(domain, port, TEST_IP)
    socket.getaddrinfo = new_getaddrinfo
    # Use a prepared request, to disable path normalization.
    # We need this because some test checks requests with paths like
    # /test-path/deep/.././deeper but we don't want the client to send
    # /test-path/deeper
    # See also https://github.com/psf/requests/issues/5289
    url = 'https://%s:%s/%s' % (domain, port, path)
    req = requests.Request(
        method='GET',
        url=url,
        headers=headers,
        cookies=cookies,
    )
    prepped = req.prepare()
    prepped.url = url
    return session.send(prepped, verify=False, allow_redirects=False)
  finally:
    socket.getaddrinfo = socket_getaddrinfo


def fakeHTTPResult(domain, path, port=HTTP_PORT,
                   headers=None, source_ip=SOURCE_IP):
  if headers is None:
    headers = {}
  # workaround request problem of setting Accept-Encoding
  # https://github.com/requests/requests/issues/2234
  headers.setdefault('Accept-Encoding', 'dummy')
  # Headers to tricks the whole system, like rouge user would do
  headers.setdefault('X-Forwarded-For', '192.168.0.1')
  headers.setdefault('X-Forwarded-Proto', 'irc')
  headers.setdefault('X-Forwarded-Port', '17')
  headers['Host'] = '%s:%s' % (domain, port)
  session = requests.Session()
  if source_ip is not None:
    new_source = source.SourceAddressAdapter(source_ip)
    session.mount('http://', new_source)
    session.mount('https://', new_source)

  # Use a prepared request, to disable path normalization.
  url = 'http://%s:%s/%s' % (TEST_IP, port, path)
  req = requests.Request(method='GET', url=url, headers=headers)
  prepped = req.prepare()
  prepped.url = url
  return session.send(prepped, allow_redirects=False)


class TestHandler(BaseHTTPRequestHandler):
  identification = None
  configuration = {}

  def log_message(self, *args):
    if os.environ.get('SLAPOS_TEST_DEBUG'):
      return BaseHTTPRequestHandler.log_message(self, *args)
    else:
      return

  def do_DELETE(self):
    config = self.configuration.pop(self.path, None)
    if config is None:
      self.send_response(204)
      self.end_headers()
    else:
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.end_headers()
      self.wfile.write(json.dumps({self.path: config}, indent=2))

  def do_PUT(self):
    config = {
      'status_code': self.headers.dict.get('x-reply-status-code', '200')
    }
    prefix = 'x-reply-header-'
    length = len(prefix)
    for key, value in self.headers.dict.items():
      if key.startswith(prefix):
        header = '-'.join([q.capitalize() for q in key[length:].split('-')])
        config[header] = value.strip()

    if 'x-reply-body' in self.headers.dict:
      config['Body'] = base64.b64decode(self.headers.dict['x-reply-body'])

    config['X-Drop-Header'] = self.headers.dict.get('x-drop-header')
    self.configuration[self.path] = config

    self.send_response(201)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(json.dumps({self.path: config}, indent=2))

  def do_POST(self):
    return self.do_GET()

  def do_GET(self):
    config = self.configuration.get(self.path, None)
    if config is not None:
      config = config.copy()
      response = config.pop('Body', None)
      status_code = int(config.pop('status_code'))
      timeout = int(config.pop('Timeout', '0'))
      compress = int(config.pop('Compress', '0'))
      drop_header_list = []
      for header in config.pop('X-Drop-Header', '').split():
        drop_header_list.append(header)
      header_dict = config
    else:
      drop_header_list = []
      for header in self.headers.dict.get('x-drop-header', '').split():
        drop_header_list.append(header)
      response = None
      status_code = 200
      timeout = int(self.headers.dict.get('timeout', '0'))
      if 'x-maximum-timeout' in self.headers.dict:
        maximum_timeout = int(self.headers.dict['x-maximum-timeout'])
        timeout = random.randrange(maximum_timeout)
      if 'x-response-size' in self.headers.dict:
        min_response, max_response = [
          int(q) for q in self.headers.dict['x-response-size'].split(' ')]
        reponse_size = random.randrange(min_response, max_response)
        response = ''.join(
          random.choice(string.lowercase) for x in range(reponse_size))
      compress = int(self.headers.dict.get('compress', '0'))
      header_dict = {}
      prefix = 'x-reply-header-'
      length = len(prefix)
      for key, value in self.headers.dict.items():
        if key.startswith(prefix):
          header = '-'.join([q.capitalize() for q in key[length:].split('-')])
          header_dict[header] = value.strip()
    if response is None:
      if 'x-reply-body' not in self.headers.dict:
        response = {
          'Path': self.path,
          'Incoming Headers': self.headers.dict
        }
        response = json.dumps(response, indent=2)
      else:
        response = base64.b64decode(self.headers.dict['x-reply-body'])

    time.sleep(timeout)
    self.send_response(status_code)

    for key, value in header_dict.items():
      self.send_header(key, value)

    if self.identification is not None:
      self.send_header('X-Backend-Identification', self.identification)

    if 'Content-Type' not in drop_header_list:
      self.send_header("Content-Type", "application/json")
    if 'Set-Cookie' not in drop_header_list:
      self.send_header('Set-Cookie', 'secured=value;secure')
      self.send_header('Set-Cookie', 'nonsecured=value')

    if compress:
      self.send_header('Content-Encoding', 'gzip')
      out = StringIO.StringIO()
      # compress with level 0, to find out if in the middle someting would
      # like to alter the compression
      with gzip.GzipFile(fileobj=out, mode="w", compresslevel=0) as f:
        f.write(response)
      response = out.getvalue()
      self.send_header('Backend-Content-Length', len(response))
    if 'Content-Length' not in drop_header_list:
      self.send_header('Content-Length', len(response))
    self.end_headers()
    self.wfile.write(response)


class HttpFrontendTestCase(SlapOSInstanceTestCase):
  # show full diffs, as it is required for proper analysis of problems
  maxDiff = None

  # minimise partition path
  __partition_reference__ = 'T-'

  @classmethod
  def prepareCertificate(cls):
    cls.another_server_ca = CertificateAuthority("Another Server Root CA")
    cls.test_server_ca = CertificateAuthority("Test Server Root CA")
    key, key_pem, csr, csr_pem = createCSR(
      "testserver.example.com", cls._ipv4_address)
    _, cls.test_server_certificate_pem = cls.test_server_ca.signCSR(csr)

    cls.test_server_certificate_file = tempfile.NamedTemporaryFile(
      delete=False
    )

    cls.test_server_certificate_file.write(
        cls.test_server_certificate_pem + key_pem
      )
    cls.test_server_certificate_file.close()

  @classmethod
  def startServerProcess(cls):
    server = ThreadedHTTPServer(
      (cls._ipv4_address, cls._server_http_port),
      TestHandler)

    server_https = ThreadedHTTPServer(
      (cls._ipv4_address, cls._server_https_port),
      TestHandler)

    server_https.socket = ssl.wrap_socket(
      server_https.socket,
      certfile=cls.test_server_certificate_file.name,
      server_side=True)

    cls.backend_url = 'http://%s:%s/' % server.server_address
    cls.server_process = multiprocessing.Process(
      target=server.serve_forever, name='HTTPServer')
    cls.server_process.start()
    cls.logger.debug('Started process %s' % (cls.server_process,))

    cls.backend_https_url = 'https://%s:%s/' % server_https.server_address
    cls.server_https_process = multiprocessing.Process(
      target=server_https.serve_forever, name='HTTPSServer')
    cls.server_https_process.start()
    cls.logger.debug('Started process %s' % (cls.server_https_process,))

  @classmethod
  def cleanUpCertificate(cls):
    if getattr(cls, 'test_server_certificate_file', None) is not None:
      os.unlink(cls.test_server_certificate_file.name)

  @classmethod
  def stopServerProcess(cls):
    for server in ['server_process', 'server_https_process']:
      process = getattr(cls, server, None)
      if process is not None:
        cls.logger.debug('Stopping process %s' % (process,))
        process.join(10)
        process.terminate()
        time.sleep(0.1)
        if process.is_alive():
          cls.logger.warning(
            'Process %s still alive' % (process, ))

  def startAuthenticatedServerProcess(self):
    master_parameter_dict = self.parseConnectionParameterDict()
    caucase_url = master_parameter_dict['backend-client-caucase-url']
    ca_certificate = requests.get(caucase_url + '/cas/crt/ca.crt.pem')
    assert ca_certificate.status_code == httplib.OK
    ca_certificate_file = os.path.join(
      self.working_directory, 'ca-backend-client.crt.pem')
    with open(ca_certificate_file, 'w') as fh:
      fh.write(ca_certificate.text)

    class OwnTestHandler(TestHandler):
      identification = 'Auth Backend'

    server_https_auth = ThreadedHTTPServer(
      (self._ipv4_address, self._server_https_auth_port),
      OwnTestHandler)

    server_https_auth.socket = ssl.wrap_socket(
      server_https_auth.socket,
      certfile=self.test_server_certificate_file.name,
      cert_reqs=ssl.CERT_REQUIRED,
      ca_certs=ca_certificate_file,
      server_side=True)

    self.backend_https_auth_url = 'https://%s:%s/' \
        % server_https_auth.server_address

    self.server_https_auth_process = multiprocessing.Process(
      target=server_https_auth.serve_forever, name='HTTPSServerAuth')
    self.server_https_auth_process.start()
    self.logger.debug('Started process %s' % (self.server_https_auth_process,))

  def stopAuthenticatedServerProcess(self):
    self.logger.debug('Stopping process %s' % (
      self.server_https_auth_process,))
    self.server_https_auth_process.join(10)
    self.server_https_auth_process.terminate()
    time.sleep(0.1)
    if self.server_https_auth_process.is_alive():
      self.logger.warning(
        'Process %s still alive' % (self.server_https_auth_process, ))

  @classmethod
  def setUpMaster(cls):
    # run partition until AIKC finishes
    cls.runComputerPartitionUntil(
      cls.untilNotReadyYetNotInMasterKeyGenerateAuthUrl)
    parameter_dict = cls.requestDefaultInstance().getConnectionParameterDict()
    ca_certificate = requests.get(
      parameter_dict['kedifa-caucase-url'] + '/cas/crt/ca.crt.pem')
    assert ca_certificate.status_code == httplib.OK
    cls.ca_certificate_file = os.path.join(cls.working_directory, 'ca.crt.pem')
    open(cls.ca_certificate_file, 'w').write(ca_certificate.text)
    auth = requests.get(
      parameter_dict['master-key-generate-auth-url'],
      verify=cls.ca_certificate_file)
    assert auth.status_code == httplib.CREATED
    upload = requests.put(
      parameter_dict['master-key-upload-url'] + auth.text,
      data=cls.key_pem + cls.certificate_pem,
      verify=cls.ca_certificate_file)
    assert upload.status_code == httplib.CREATED
    cls.runKedifaUpdater()

  @classmethod
  def runKedifaUpdater(cls):
    kedifa_updater = None
    for kedifa_updater in sorted(glob.glob(
        os.path.join(
          cls.instance_path, '*', 'etc', 'service', 'kedifa-updater*'))):
      # fetch first kedifa-updater, as by default most of the tests are using
      # only one running partition; in case if test does not need
      # kedifa-updater this method can be overridden
      break
    if kedifa_updater is not None:
      # try few times kedifa_updater
      for i in range(10):
        return_code, output = subprocess_status_output(
          [kedifa_updater, '--once'])
        if return_code == 0:
          break
        # wait for the other updater to work
        time.sleep(2)
      # assert that in the worst case last run was correct
      assert return_code == 0, output
      # give caddy a moment to refresh its config, as sending signal does not
      # block until caddy is refreshed
      time.sleep(2)

  @classmethod
  def createWildcardExampleComCertificate(cls):
    _, cls.key_pem, _, cls.certificate_pem = createSelfSignedCertificate(
      [
        '*.customdomain.example.com',
        '*.example.com',
        '*.alias1.example.com',
      ])

  @classmethod
  def runComputerPartitionUntil(cls, until):
    max_try = 10
    try_num = 1
    while True:
      if until():
        break
      if try_num > max_try:
        raise ValueError('Failed to run computer partition with %r' % (until,))
      try:
        cls.slap.waitForInstance()
      except Exception:
        cls.logger.exception("Error during until run")
      try_num += 1

  @classmethod
  def untilNotReadyYetNotInMasterKeyGenerateAuthUrl(cls):
    parameter_dict = cls.requestDefaultInstance().getConnectionParameterDict()
    key = 'master-key-generate-auth-url'
    if key not in parameter_dict:
      return False
    if 'NotReadyYet' in parameter_dict[key]:
      return False
    return True

  @classmethod
  def callSupervisorMethod(cls, method, *args, **kwargs):
    with cls.slap.instance_supervisor_rpc as instance_supervisor:
      return getattr(instance_supervisor, method)(*args, **kwargs)

  def assertRejectedSlavePromiseWithPop(self, parameter_dict):
    rejected_slave_promise_url = parameter_dict.pop(
      'rejected-slave-promise-url')

    try:
      result = requests.get(rejected_slave_promise_url, verify=False)
      if result.text == '':
        result_json = {}
      else:
        result_json = result.json()
      self.assertEqual(
        parameter_dict['rejected-slave-dict'],
        result_json
      )
    except AssertionError:
      raise
    except Exception as e:
      self.fail(e)

  def assertLogAccessUrlWithPop(self, parameter_dict):
    log_access_url = parameter_dict.pop('log-access-url')

    self.assertTrue(len(log_access_url) >= 1)
    # check only the first one, as second frontend will be stopped
    log_access = log_access_url[0]
    entry = log_access.split(': ')
    if len(entry) != 2:
      self.fail('Cannot parse %r' % (log_access,))
    frontend, url = entry
    result = requests.get(url, verify=False)
    self.assertEqual(
      httplib.OK,
      result.status_code,
      'While accessing %r of %r the status code was %r' % (
        url, frontend, result.status_code))
    # check that the result is correct JSON, which allows to access
    # information about all logs
    self.assertEqual(
      'application/json',
      result.headers['Content-Type']
    )
    self.assertEqual(
      sorted([q['name'] for q in result.json()]),
      ['access.log', 'backend.log', 'error.log'])
    self.assertEqual(
      httplib.OK,
      requests.get(url + 'access.log', verify=False).status_code
    )
    self.assertEqual(
      httplib.OK,
      requests.get(url + 'error.log', verify=False).status_code
    )
    # assert only for few tests, as backend log is not available for many of
    # them, as it's created on the fly
    for test_name in [
      'test_url', 'test_auth_to_backend', 'test_compressed_result']:
      if self.id().endswith(test_name):
        self.assertEqual(
          httplib.OK,
          requests.get(url + 'backend.log', verify=False).status_code
        )

  def assertKedifaKeysWithPop(self, parameter_dict, prefix=''):
    generate_auth_url = parameter_dict.pop('%skey-generate-auth-url' % (
      prefix,))
    upload_url = parameter_dict.pop('%skey-upload-url' % (prefix,))
    kedifa_ipv6_base = 'https://[%s]:%s' % (self._ipv6_address, KEDIFA_PORT)
    base = '^' + kedifa_ipv6_base.replace(
      '[', r'\[').replace(']', r'\]') + '/.{32}'
    self.assertRegexpMatches(
      generate_auth_url,
      base + r'\/generateauth$'
    )
    self.assertRegexpMatches(
      upload_url,
      base + r'\?auth=$'
    )

    kedifa_caucase_url = parameter_dict.pop('kedifa-caucase-url')
    self.assertEqual(
      kedifa_caucase_url,
      'http://[%s]:%s' % (self._ipv6_address, CAUCASE_PORT),
    )

    return generate_auth_url, upload_url

  def assertBackendHaproxyStatisticUrl(self, parameter_dict):
    url_key = 'caddy-frontend-1-backend-haproxy-statistic-url'
    backend_haproxy_statistic_url_dict = {}
    for key in parameter_dict.keys():
      if key.startswith('caddy-frontend') and key.endswith(
        'backend-haproxy-statistic-url'):
        backend_haproxy_statistic_url_dict[key] = parameter_dict.pop(key)
    self.assertEqual(
      [url_key],
      backend_haproxy_statistic_url_dict.keys()
    )

    backend_haproxy_statistic_url = backend_haproxy_statistic_url_dict[url_key]
    result = requests.get(
      backend_haproxy_statistic_url,
      verify=False,
    )
    self.assertEqual(httplib.OK, result.status_code)
    self.assertIn('testing partition 0', result.text)
    self.assertIn('Statistics Report for HAProxy', result.text)

  def assertKeyWithPop(self, key, d):
    self.assertTrue(key in d, 'Key %r is missing in %r' % (key, d))
    d.pop(key)

  def assertEqualResultJson(self, result, key, value):
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertTrue(key in j, 'No key %r in %s' % (key, j))
    self.assertEqual(value, j[key])

  def patchRequests(self):
    HTTPResponse = requests.packages.urllib3.response.HTTPResponse
    HTTPResponse.orig__init__ = HTTPResponse.__init__

    def new_HTTPResponse__init__(self, *args, **kwargs):
      self.orig__init__(*args, **kwargs)
      try:
        self.peercert = self._connection.sock.getpeercert(binary_form=True)
      except AttributeError:
        pass
    HTTPResponse.__init__ = new_HTTPResponse__init__

    HTTPAdapter = requests.adapters.HTTPAdapter
    HTTPAdapter.orig_build_response = HTTPAdapter.build_response

    def new_HTTPAdapter_build_response(self, request, resp):
      response = self.orig_build_response(request, resp)
      try:
        response.peercert = resp.peercert
      except AttributeError:
        pass
      return response
    HTTPAdapter.build_response = new_HTTPAdapter_build_response

  def unpatchRequests(self):
    HTTPResponse = requests.packages.urllib3.response.HTTPResponse
    if getattr(HTTPResponse, 'orig__init__', None) is not None:
      HTTPResponse.__init__ = HTTPResponse.orig__init__
      del(HTTPResponse.orig__init__)

    HTTPAdapter = requests.adapters.HTTPAdapter
    if getattr(HTTPAdapter, 'orig_build_response', None) is not None:
      HTTPAdapter.build_response = HTTPAdapter.orig_build_response
      del(HTTPAdapter.orig_build_response)

  def setUp(self):
    # patch requests in order to being able to extract SSL certs
    self.patchRequests()

  def tearDown(self):
    self.unpatchRequests()
    super(HttpFrontendTestCase, self).tearDown()

  def parseParameterDict(self, parameter_dict):
    parsed_parameter_dict = {}
    for key, value in parameter_dict.items():
      if key in [
        'rejected-slave-dict',
        'warning-slave-dict',
        'warning-list',
        'request-error-list',
        'log-access-url']:
        value = json.loads(value)
      parsed_parameter_dict[key] = value
    return parsed_parameter_dict

  def getMasterPartitionPath(self):
    # partition w/o etc/trafficserver, but with buildout.cfg
    return [
      q for q in glob.glob(os.path.join(self.instance_path, '*',))
      if not os.path.exists(
        os.path.join(q, 'etc', 'trafficserver')) and os.path.exists(
          os.path.join(q, 'buildout.cfg'))][0]

  def parseConnectionParameterDict(self):
    return self.parseParameterDict(
      self.requestDefaultInstance().getConnectionParameterDict()
    )

  @classmethod
  def waitForMethod(cls, name, method):
    wait_time = 600
    begin = time.time()
    try_num = 0
    cls.logger.debug('%s for %is' % (name, wait_time,))
    while True:
      try:
        try_num += 1
        method()
      except Exception:
        if time.time() - begin > wait_time:
          cls.logger.exception(
            "Error during %s after %.2fs" % (name, (time.time() - begin),))
          raise
        else:
          time.sleep(0.5)
      else:
        cls.logger.info("%s took %.2fs" % (name, (time.time() - begin),))
        break

  @classmethod
  def waitForCaddy(cls):
    def method():
      fakeHTTPSResult(
        cls._ipv4_address,
        '/',
      )
    cls.waitForMethod('waitForCaddy', method)

  @classmethod
  def _cleanup(cls, snapshot_name):
    cls.cleanUpCertificate()
    cls.stopServerProcess()
    super(HttpFrontendTestCase, cls)._cleanup(snapshot_name)

  @classmethod
  def setUpClass(cls):
    try:
      cls.createWildcardExampleComCertificate()
      cls.prepareCertificate()
      # find ports once to be able startServerProcess many times
      cls._server_http_port = findFreeTCPPort(cls._ipv4_address)
      cls._server_https_port = findFreeTCPPort(cls._ipv4_address)
      cls._server_https_auth_port = findFreeTCPPort(cls._ipv4_address)
      cls.startServerProcess()
    except BaseException:
      cls.logger.exception("Error during setUpClass")
      cls._cleanup("{}.{}.setUpClass".format(cls.__module__, cls.__name__))
      cls.setUp = lambda self: self.fail('Setup Class failed.')
      raise

    super(HttpFrontendTestCase, cls).setUpClass()

    try:
      # expose instance directory
      cls.instance_path = cls.slap.instance_directory
      # expose software directory, extract from found computer partition
      cls.software_path = os.path.realpath(os.path.join(
          cls.computer_partition_root_path, 'software_release'))
      # do working directory
      cls.working_directory = os.path.join(os.path.realpath(
          os.environ.get(
              'SLAPOS_TEST_WORKING_DIR',
              os.path.join(os.getcwd(), '.slapos'))),
          'caddy-frontend-test')
      if not os.path.isdir(cls.working_directory):
        os.mkdir(cls.working_directory)
      cls.setUpMaster()
      cls.waitForCaddy()
    except BaseException:
      cls.logger.exception("Error during setUpClass")
      # "{}.{}.setUpClass".format(cls.__module__, cls.__name__) is already used
      # by SlapOSInstanceTestCase.setUpClass so we use another name for
      # snapshot, to make sure we don't store another snapshot in same
      # directory.
      cls._cleanup("{}.SlaveHttpFrontendTestCase.{}.setUpClass".format(
        cls.__module__, cls.__name__))
      cls.setUp = lambda self: self.fail('Setup Class failed.')
      raise


class SlaveHttpFrontendTestCase(HttpFrontendTestCase):
  @classmethod
  def requestDefaultInstance(cls, state='started'):
    default_instance = super(
      SlaveHttpFrontendTestCase, cls).requestDefaultInstance(state=state)
    if state != 'destroyed':
      cls.requestSlaves()
    return default_instance

  @classmethod
  def requestSlaveInstance(cls, partition_reference, partition_parameter_kw):
    software_url = cls.getSoftwareURL()
    software_type = cls.getInstanceSoftwareType()
    cls.logger.debug(
      'requesting slave "%s" type: %r software:%s parameters:%s',
      partition_reference, software_type, software_url, partition_parameter_kw)
    return cls.slap.request(
      software_release=software_url,
      software_type=software_type,
      partition_reference=partition_reference,
      partition_parameter_kw=partition_parameter_kw,
      shared=True
    )

  @classmethod
  def requestSlaves(cls):
    for slave_reference, partition_parameter_kw in cls\
            .getSlaveParameterDictDict().items():
      software_url = cls.getSoftwareURL()
      software_type = cls.getInstanceSoftwareType()
      cls.logger.debug(
        'requesting slave "%s" type: %r software:%s parameters:%s',
        slave_reference, software_type, software_url, partition_parameter_kw)
      cls.requestSlaveInstance(
        partition_reference=slave_reference,
        partition_parameter_kw=partition_parameter_kw,
      )

  @classmethod
  def setUpClass(cls):
    super(SlaveHttpFrontendTestCase, cls).setUpClass()

    try:
      cls.setUpSlaves()
      cls.waitForSlave()
    except BaseException:
      cls.logger.exception("Error during setUpClass")
      # "{}.{}.setUpClass".format(cls.__module__, cls.__name__) is already used
      # by SlapOSInstanceTestCase.setUpClass so we use another name for
      # snapshot, to make sure we don't store another snapshot in same
      # directory.
      cls._cleanup("{}.SlaveHttpFrontendTestCase.{}.setUpClass".format(
        cls.__module__, cls.__name__))
      cls.setUp = lambda self: self.fail('Setup Class failed.')
      raise

  @classmethod
  def waitForSlave(cls):
    def method():
      for parameter_dict in cls.getSlaveConnectionParameterDictList():
        if 'domain' in parameter_dict:
          try:
            fakeHTTPSResult(
              parameter_dict['domain'], '/')
          except requests.exceptions.InvalidURL:
            # ignore slaves to which connection is impossible by default
            continue
    cls.waitForMethod('waitForSlave', method)

  @classmethod
  def getSlaveConnectionParameterDictList(cls):
    parameter_dict_list = []

    for slave_reference, partition_parameter_kw in cls\
            .getSlaveParameterDictDict().items():
      parameter_dict_list.append(cls.requestSlaveInstance(
        partition_reference=slave_reference,
        partition_parameter_kw=partition_parameter_kw,
      ).getConnectionParameterDict())
    return parameter_dict_list

  @classmethod
  def untilSlavePartitionReady(cls):
    # all on-watch services shall not be exited
    for process in cls.callSupervisorMethod('getAllProcessInfo'):
      if process['name'].endswith('-on-watch') and \
        process['statename'] == 'EXITED':
        if process['name'].startswith('monitor-http'):
          continue
        return False

    for parameter_dict in cls.getSlaveConnectionParameterDictList():
      log_access_ready = 'log-access-url' in parameter_dict
      key = 'key-generate-auth-url'
      key_generate_auth_ready = key in parameter_dict \
          and 'NotReadyYet' not in parameter_dict[key]
      if not(log_access_ready and key_generate_auth_ready):
        return False

    return True

  @classmethod
  def setUpSlaves(cls):
    cls.runComputerPartitionUntil(
      cls.untilSlavePartitionReady)
    cls.updateSlaveConnectionParameterDictDict()

  @classmethod
  def updateSlaveConnectionParameterDictDict(cls):
    cls.slave_connection_parameter_dict_dict = {}
    # run partition for slaves to be setup
    for slave_reference, partition_parameter_kw in cls\
            .getSlaveParameterDictDict().items():
      slave_instance = cls.requestSlaveInstance(
        partition_reference=slave_reference,
        partition_parameter_kw=partition_parameter_kw,
      )
      cls.slave_connection_parameter_dict_dict[slave_reference] = \
          slave_instance.getConnectionParameterDict()

  def parseSlaveParameterDict(self, key):
    return self.parseParameterDict(
      self.slave_connection_parameter_dict_dict[
        key
      ]
    )

  def assertSlaveBase(self, reference):
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = reference.translate(None, '_-').lower()
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    return parameter_dict

  def assertLastLogLineRegexp(self, log_name, log_regexp):
    log_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'var', 'log', 'httpd', log_name
      ))[0]

    self.assertRegexpMatches(
      open(log_file, 'r').readlines()[-1],
      log_regexp)


class TestMasterRequestDomain(HttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  def test(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    self.assertEqual(
      {
        'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'domain': 'example.com',
        'accepted-slave-amount': '0',
        'rejected-slave-amount': '0',
        'slave-amount': '0',
        'rejected-slave-dict': {}
      },
      parameter_dict
    )


class TestMasterRequest(HttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  def test(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)
    self.assertEqual(
      {
        'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'domain': 'None',
        'accepted-slave-amount': '0',
        'rejected-slave-amount': '0',
        'slave-amount': '0',
        'rejected-slave-dict': {}},
      parameter_dict
    )


class TestSlave(SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
      'request-timeout': '12',
    }

  @classmethod
  def prepareCertificate(cls):
    cls.ca = CertificateAuthority('TestSlave')
    _, cls.customdomain_ca_key_pem, csr, _ = createCSR(
      'customdomainsslcrtsslkeysslcacrt.example.com')
    _, cls.customdomain_ca_certificate_pem = cls.ca.signCSR(csr)
    _, cls.customdomain_key_pem, _, cls.customdomain_certificate_pem = \
        createSelfSignedCertificate(['customdomainsslcrtsslkey.example.com'])
    super(TestSlave, cls).prepareCertificate()

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'empty': {
      },
      'Url': {
        # make URL "incorrect", with whitespace, nevertheless it shall be
        # correctly handled
        'url': ' ' + cls.backend_url + '/?a=b&c=' + ' ',
        # authenticating to http backend shall be no-op
        'authenticate-to-backend': True,
      },
      'auth-to-backend': {
        # in here use reserved port for the backend, which is going to be
        # started later
        'url': 'https://%s:%s/' % (
          cls._ipv4_address, cls._server_https_auth_port),
        'authenticate-to-backend': True,
      },
      'auth-to-backend-not-configured': {
        # in here use reserved port for the backend, which is going to be
        # started later
        'url': 'https://%s:%s/' % (
          cls._ipv4_address, cls._server_https_auth_port),
      },
      'auth-to-backend-backend-ignore': {
        'url': cls.backend_https_url,
        'authenticate-to-backend': True,
      },
      'url_https-url': {
        'url': cls.backend_url + 'http',
        'https-url': cls.backend_url + 'https',
        'backend-connect-timeout': 10,
        'backend-connect-retries': 5,
        'request-timeout': 15,
        'strict-transport-security': '200',
        'strict-transport-security-sub-domains': True,
        'strict-transport-security-preload': True,
      },
      'server-alias': {
        'url': cls.backend_url,
        'server-alias': 'alias1.example.com alias2.example.com',
        'strict-transport-security': '200',
      },
      'server-alias-empty': {
        'url': cls.backend_url,
        'server-alias': '',
        'strict-transport-security': '200',
        'strict-transport-security-sub-domains': True,
      },
      'server-alias-wildcard': {
        'url': cls.backend_url,
        'server-alias': '*.alias1.example.com',
        'strict-transport-security': '200',
        'strict-transport-security-preload': True,
      },
      'server-alias-duplicated': {
        'url': cls.backend_url,
        'server-alias': 'alias3.example.com alias3.example.com',
      },
      'server-alias_custom_domain-duplicated': {
        'url': cls.backend_url,
        'custom_domain': 'alias4.example.com',
        'server-alias': 'alias4.example.com alias4.example.com',
      },
      'ssl-proxy-verify_ssl_proxy_ca_crt': {
        'url': cls.backend_https_url,
        'ssl-proxy-verify': True,
        'ssl_proxy_ca_crt': cls.test_server_ca.certificate_pem,
      },
      'ssl-proxy-verify_ssl_proxy_ca_crt-unverified': {
        'url': cls.backend_https_url,
        'ssl-proxy-verify': True,
        'ssl_proxy_ca_crt': cls.another_server_ca.certificate_pem,
      },
      'ssl-proxy-verify-unverified': {
        'url': cls.backend_https_url,
        'ssl-proxy-verify': True,
      },
      'https-only': {
        'url': cls.backend_url,
        'https-only': False,
      },
      'custom_domain': {
        'url': cls.backend_url,
        'custom_domain': 'mycustomdomain.example.com',
      },
      'custom_domain_wildcard': {
        'url': cls.backend_url,
        'custom_domain': '*.customdomain.example.com',
      },
      'custom_domain_server_alias': {
        'url': cls.backend_url,
        'custom_domain': 'mycustomdomainserveralias.example.com',
        'server-alias': 'mycustomdomainserveralias1.example.com',
      },
      'custom_domain_ssl_crt_ssl_key': {
        'url': cls.backend_url,
        'custom_domain': 'customdomainsslcrtsslkey.example.com',
      },
      'custom_domain_ssl_crt_ssl_key_ssl_ca_crt': {
        'url': cls.backend_url,
        'custom_domain': 'customdomainsslcrtsslkeysslcacrt.example.com',
      },
      'ssl_ca_crt_only': {
        'url': cls.backend_url,
      },
      'ssl_ca_crt_garbage': {
        'url': cls.backend_url,
      },
      'ssl_ca_crt_does_not_match': {
        'url': cls.backend_url,
      },
      'type-zope': {
        'url': cls.backend_url,
        'type': 'zope',
      },
      'type-zope-prefer-gzip-encoding-to-backend': {
        'url': cls.backend_url,
        'prefer-gzip-encoding-to-backend': 'true',
        'type': 'zope',
      },
      'type-zope-prefer-gzip-encoding-to-backend-https-only': {
        'url': cls.backend_url,
        'prefer-gzip-encoding-to-backend': 'true',
        'type': 'zope',
        'https-only': 'false',
      },
      'type-zope-virtualhostroot-http-port': {
        'url': cls.backend_url,
        'type': 'zope',
        'virtualhostroot-http-port': '12345',
        'https-only': 'false',
      },
      'type-zope-virtualhostroot-https-port': {
        'url': cls.backend_url,
        'type': 'zope',
        'virtualhostroot-https-port': '12345'
      },
      'type-zope-path': {
        'url': cls.backend_url,
        'type': 'zope',
        'path': '///path/to/some/resource///',
      },
      'type-zope-default-path': {
        'url': cls.backend_url,
        'type': 'zope',
        'default-path': '///default-path/to/some/resource///',
      },
      'type-notebook': {
        'url': cls.backend_url,
        'type': 'notebook',
      },
      'type-websocket': {
        'url': cls.backend_url,
        'type': 'websocket',
      },
      'type-websocket-websocket-path-list': {
        'url': cls.backend_url,
        'type': 'websocket',
        'websocket-path-list': '////ws//// /with%20space/',
      },
      'type-websocket-websocket-transparent-false': {
        'url': cls.backend_url,
        'type': 'websocket',
        'websocket-transparent': 'false',
      },
      'type-websocket-websocket-path-list-websocket-transparent-false': {
        'url': cls.backend_url,
        'type': 'websocket',
        'websocket-path-list': '////ws//// /with%20space/',
        'websocket-transparent': 'false',
      },
      # 'type-eventsource': {
      #   'url': cls.backend_url,
      #   'type': 'eventsource',
      # },
      'type-redirect': {
        'url': cls.backend_url,
        'type': 'redirect',
      },
      'type-redirect-custom_domain': {
        'url': cls.backend_url,
        'type': 'redirect',
        'custom_domain': 'customdomaintyperedirect.example.com',
      },
      'enable_cache': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
      'enable_cache_custom_domain': {
        'url': cls.backend_url,
        'enable_cache': True,
        'custom_domain': 'customdomainenablecache.example.com',
      },
      'enable_cache_server_alias': {
        'url': cls.backend_url,
        'enable_cache': True,
        'server-alias': 'enablecacheserveralias1.example.com',
      },
      'enable_cache-disable-no-cache-request': {
        'url': cls.backend_url,
        'enable_cache': True,
        'disable-no-cache-request': True,
      },
      'enable_cache-disable-via-header': {
        'url': cls.backend_url,
        'enable_cache': True,
        'disable-via-header': True,
      },
      'enable_cache-https-only': {
        'url': cls.backend_url,
        'https-only': False,
        'enable_cache': True,
      },
      'enable-http2-false': {
        'url': cls.backend_url,
        'enable-http2': False,
      },
      'enable-http2-default': {
        'url': cls.backend_url,
      },
      'prefer-gzip-encoding-to-backend': {
        'url': cls.backend_url,
        'prefer-gzip-encoding-to-backend': 'true',
      },
      'prefer-gzip-encoding-to-backend-https-only': {
        'url': cls.backend_url,
        'prefer-gzip-encoding-to-backend': 'true',
        'https-only': 'false',
      },
      'disabled-cookie-list': {
        'url': cls.backend_url,
        'disabled-cookie-list': 'Chocolate Vanilia',
      },
      'monitor-ipv4-test': {
        'monitor-ipv4-test': 'monitor-ipv4-test',
      },
      'monitor-ipv6-test': {
        'monitor-ipv6-test': 'monitor-ipv6-test',
      },
      'ciphers': {
        'ciphers': 'RSA-3DES-EDE-CBC-SHA RSA-AES128-CBC-SHA',
      }
    }

  monitor_setup_url_key = 'monitor-setup-url'

  def test_monitor_setup(self):
    IP = self._ipv6_address
    self.monitor_configuration_list = [
      {
        'htmlUrl': 'https://[%s]:8401/public/feed' % (IP,),
        'text': 'testing partition 0',
        'title': 'testing partition 0',
        'type': 'rss',
        'url': 'https://[%s]:8401/share/private/' % (IP,),
        'version': 'RSS',
        'xmlUrl': 'https://[%s]:8401/public/feed' % (IP,),
      },
      {
        'htmlUrl': 'https://[%s]:8402/public/feed' % (IP,),
        'text': 'kedifa',
        'title': 'kedifa',
        'type': 'rss',
        'url': 'https://[%s]:8402/share/private/' % (IP,),
        'version': 'RSS',
        'xmlUrl': 'https://[%s]:8402/public/feed' % (IP,),
      },
      {
        'htmlUrl': 'https://[%s]:8411/public/feed' % (IP,),
        'text': 'caddy-frontend-1',
        'title': 'caddy-frontend-1',
        'type': 'rss',
        'url': 'https://[%s]:8411/share/private/' % (IP,),
        'version': 'RSS',
        'xmlUrl': 'https://[%s]:8411/public/feed' % (IP,),
      },
    ]
    connection_parameter_dict = self\
        .computer_partition.getConnectionParameterDict()
    self.assertTrue(
      self.monitor_setup_url_key in connection_parameter_dict,
      '%s not in %s' % (self.monitor_setup_url_key, connection_parameter_dict))
    monitor_setup_url_value = connection_parameter_dict[
      self.monitor_setup_url_key]
    monitor_url_match = re.match(r'.*url=(.*)', monitor_setup_url_value)
    self.assertNotEqual(
      None, monitor_url_match, '%s not parsable' % (monitor_setup_url_value,))
    self.assertEqual(1, len(monitor_url_match.groups()))
    monitor_url = monitor_url_match.groups()[0]
    monitor_url_split = monitor_url.split('&')
    self.assertEqual(
      3, len(monitor_url_split), '%s not splitabble' % (monitor_url,))
    self.monitor_url = monitor_url_split[0]
    monitor_username = monitor_url_split[1].split('=')
    self.assertEqual(
      2, len(monitor_username), '%s not splittable' % (monitor_username))
    monitor_password = monitor_url_split[2].split('=')
    self.assertEqual(
      2, len(monitor_password), '%s not splittable' % (monitor_password))
    self.monitor_username = monitor_username[1]
    self.monitor_password = monitor_password[1]

    opml_text = requests.get(self.monitor_url, verify=False).text
    opml = ET.fromstring(opml_text)

    body = opml[1]
    self.assertEqual('body', body.tag)

    outline_list = body[0].findall('outline')

    self.assertEqual(
      self.monitor_configuration_list,
      [q.attrib for q in outline_list]
    )

    expected_status_code_list = []
    got_status_code_list = []
    for monitor_configuration in self.monitor_configuration_list:
      status_code = requests.get(
          monitor_configuration['url'],
          verify=False,
          auth=(self.monitor_username, self.monitor_password)
        ).status_code
      expected_status_code_list.append(
        {
          'url': monitor_configuration['url'],
          'status_code': 200
        }
      )
      got_status_code_list.append(
        {
          'url': monitor_configuration['url'],
          'status_code': status_code
        }
      )
    self.assertEqual(
      expected_status_code_list,
      got_status_code_list
    )

  def getSlavePartitionPath(self):
    # partition w/ etc/trafficserver
    return [
      q for q in glob.glob(os.path.join(self.instance_path, '*',))
      if os.path.exists(os.path.join(q, 'etc', 'trafficserver'))][0]

  def test_trafficserver_logrotate(self):
    ats_partition = [
      q for q in glob.glob(os.path.join(self.instance_path, '*',))
      if os.path.exists(os.path.join(q, 'bin', 'trafficserver-rotate'))][0]
    ats_log_dir = os.path.join(ats_partition, 'var', 'log', 'trafficserver')
    ats_logrotate_dir = os.path.join(
      ats_partition, 'srv', 'backup', 'logrotate', 'trafficserver')
    ats_rotate = os.path.join(ats_partition, 'bin', 'trafficserver-rotate')

    old_file_name = 'log-old.old'
    older_file_name = 'log-older.old'
    with open(os.path.join(ats_log_dir, old_file_name), 'w') as fh:
      fh.write('old')
    with open(os.path.join(ats_log_dir, older_file_name), 'w') as fh:
      fh.write('older')

    # check rotation
    result, output = subprocess_status_output([ats_rotate])

    self.assertEqual(0, result)

    self.assertEqual(
      set(['log-old.old.xz', 'log-older.old.xz']),
      set(os.listdir(ats_logrotate_dir)))
    self.assertFalse(old_file_name + '.xz' in os.listdir(ats_log_dir))
    self.assertFalse(older_file_name + '.xz' in os.listdir(ats_log_dir))

    with lzma.open(
      os.path.join(ats_logrotate_dir, old_file_name + '.xz')) as fh:
      self.assertEqual(
        'old',
        fh.read()
      )
    with lzma.open(
      os.path.join(ats_logrotate_dir, older_file_name + '.xz')) as fh:
      self.assertEqual(
        'older',
        fh.read()
      )

    # check retention
    old_time = time.time() - (400 * 24 * 3600)
    os.utime(
      os.path.join(ats_logrotate_dir, older_file_name + '.xz'),
      (old_time, old_time))
    result, output = subprocess_status_output([ats_rotate])

    self.assertEqual(0, result)
    self.assertEqual(
      ['log-old.old.xz'],
      os.listdir(ats_logrotate_dir))

  def test_master_partition_state(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    expected_parameter_dict = {
      'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
      'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      'domain': 'example.com',
      'accepted-slave-amount': '51',
      'rejected-slave-amount': '0',
      'slave-amount': '51',
      'rejected-slave-dict': {
      },
      'warning-slave-dict': {
        '_Url': [
          "slave url ' %(backend)s/?a=b&c= ' has been converted to "
          "'%(backend)s/?a=b&c='" % {'backend': self.backend_url}]}
    }

    self.assertEqual(
      expected_parameter_dict,
      parameter_dict
    )

    partition_path = self.getMasterPartitionPath()

    # check that monitor cors domains are correctly setup by file presence, as
    # we trust monitor stack being tested in proper place and it is too hard
    # to have working monitor with local proxy
    self.assertTestData(
      open(
        os.path.join(
          partition_path, 'etc', 'httpd-cors.cfg'), 'r').read().strip())

  def test_slave_partition_state(self):
    partition_path = self.getSlavePartitionPath()
    self.assertTrue(
      '-grace 2s' in
      open(os.path.join(partition_path, 'bin', 'caddy-wrapper'), 'r').read()
    )

  def test_monitor_conf(self):
    monitor_conf_list = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'monitor.conf'
      ))
    self.assertEqual(3, len(monitor_conf_list))
    expected = [(False, q) for q in monitor_conf_list]
    got = [('!py!' in open(q).read(), q) for q in monitor_conf_list]
    # check that no monitor.conf in generated configuratio has magic !py!
    self.assertEqual(
      expected,
      got
    )

  def test_empty(self):
    parameter_dict = self.assertSlaveBase('empty')
    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://empty.example.com:%s/test-path' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    # check that 404 is as configured
    result_missing = fakeHTTPSResult(
      'forsuredoesnotexists.example.com', '')
    self.assertEqual(httplib.NOT_FOUND, result_missing.status_code)
    self.assertEqual(
      """<html>
<head>
  <title>Instance not found</title>
</head>
<body>
<h1>The instance has not been found</h1>
<p>The reasons of this could be:</p>
<ul>
<li>the instance does not exists or the URL is incorrect
<ul>
<li>in this case please check the URL
</ul>
<li>the instance has been stopped
<ul>
<li>in this case please check in the SlapOS Master if the instance is """
      """started or wait a bit for it to start
</ul>
</ul>
</body>
</html>
""",
      result_missing.text
    )

  def test_server_polluted_keys_removed(self):
    buildout_file = os.path.join(
      self.getMasterPartitionPath(), 'buildout-switch-softwaretype.cfg')
    for line in [
      q for q in open(buildout_file).readlines()
      if q.startswith('config-slave-list') or q.startswith(
          'config-extra_slave_instance_list')]:
      self.assertFalse('slave_title' in line)
      self.assertFalse('slap_software_type' in line)
      self.assertFalse('connection-parameter-hash' in line)
      self.assertFalse('timestamp' in line)

  def assertBackendHeaders(
    self, backend_header_dict, domain, source_ip=SOURCE_IP, port=HTTPS_PORT,
    proto='https', ignore_header_list=None):
    if ignore_header_list is None:
      ignore_header_list = []
    if 'Host' not in ignore_header_list:
      self.assertEqual(
        backend_header_dict['host'],
        '%s:%s' % (domain, port))
    self.assertEqual(
      backend_header_dict['x-forwarded-for'],
      source_ip
    )
    self.assertEqual(
      backend_header_dict['x-forwarded-port'],
      port
    )
    self.assertEqual(
      backend_header_dict['x-forwarded-proto'],
      proto
    )

  def test_telemetry_disabled(self):
    # here we trust that telemetry not present in error log means it was
    # really disabled
    error_log_file = glob.glob(
      os.path.join(
       self.instance_path, '*', 'var', 'log', 'frontend-error.log'))[0]
    with open(error_log_file) as fh:
      self.assertNotIn('Sending telemetry', fh.read(), 'Telemetry enabled')

  def test_url(self):
    reference = 'Url'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = reference.translate(None, '_-').lower()
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          "slave url ' %s/?a=b&c= ' has been converted to '%s/?a=b&c='" % (
            self.backend_url, self.backend_url)],
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
        'User-Agent': 'TEST USER AGENT',
      }
    )

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertNotIn('Strict-Transport-Security', result.headers)
    self.assertEqualResultJson(result, 'Path', '?a=b&c=/test-path/deeper')

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))

    self.assertEqual(j['Incoming Headers']['timeout'], '10')
    self.assertFalse('Content-Encoding' in result.headers)
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqual(
      'secured=value;secure, nonsecured=value',
      result.headers['Set-Cookie']
    )

    self.assertLastLogLineRegexp(
      '_Url_access_log',
      r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3} - - '
      r'\[\d{2}\/.{3}\/\d{4}\:\d{2}\:\d{2}\:\d{2} \+\d{4}\] '
      r'"GET \/test-path\/deep\/..\/.\/deeper HTTP\/1.1" \d{3} '
      r'\d+ "-" "TEST USER AGENT" \d+'
    )

    self.assertLastLogLineRegexp(
      '_Url_backend_log',
      r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+ '
      r'\[\d{2}\/.{3}\/\d{4}\:\d{2}\:\d{2}\:\d{2}.\d{3}\] '
      r'http-backend _Url-http\/_Url-backend-http '
      r'\d+/\d+\/\d+\/\d+\/\d+ '
      r'200 \d+ - - ---- '
      r'\d+\/\d+\/\d+\/\d+\/\d+ \d+\/\d+ '
      r'"GET /test-path/deeper HTTP/1.1"'
    )

    result_http = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://url.example.com:%s/test-path/deeper' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    # check that timeouts are correctly set in the haproxy configuration
    backend_configuration_file = glob.glob(os.path.join(
      self.instance_path, '*', 'etc', 'backend-haproxy.cfg'))[0]
    with open(backend_configuration_file) as fh:
      content = fh.read()
    self.assertIn("""backend _Url-http
  timeout server 12s
  timeout connect 5s
  retries 3""", content)
    self.assertIn("""  timeout queue 60s
  timeout server 12s
  timeout client 12s
  timeout connect 5s
  retries 3""", content)
    # check that no needless entries are generated
    self.assertIn("backend _Url-http\n", content)
    self.assertNotIn("backend _Url-https\n", content)

  def test_auth_to_backend(self):
    parameter_dict = self.assertSlaveBase('auth-to-backend')

    self.startAuthenticatedServerProcess()
    try:
      # assert that you can't fetch nothing without key
      try:
        requests.get(self.backend_https_auth_url, verify=False)
      except Exception:
        pass
      else:
        self.fail(
          'Access to %r shall be not possible without certificate' % (
            self.backend_https_auth_url,))
      # check that you can access this backend via frontend
      # (so it means that auth to backend worked)
      result = fakeHTTPSResult(
        parameter_dict['domain'],
        'test-path/deep/.././deeper',
        headers={
          'Timeout': '10',  # more than default backend-connect-timeout == 5
          'Accept-Encoding': 'gzip',
        }
      )

      self.assertEqual(
        self.certificate_pem,
        der2pem(result.peercert))

      self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

      try:
        j = result.json()
      except Exception:
        raise ValueError('JSON decode problem in:\n%s' % (result.text,))

      self.assertEqual(j['Incoming Headers']['timeout'], '10')
      self.assertFalse('Content-Encoding' in result.headers)
      self.assertBackendHeaders(
         j['Incoming Headers'], parameter_dict['domain'])

      self.assertEqual(
        'secured=value;secure, nonsecured=value',
        result.headers['Set-Cookie']
      )
      # proof that proper backend was accessed
      self.assertEqual(
        'Auth Backend',
        result.headers['X-Backend-Identification']
      )
    finally:
      self.stopAuthenticatedServerProcess()

  def test_auth_to_backend_not_configured(self):
    parameter_dict = self.assertSlaveBase('auth-to-backend-not-configured')
    self.startAuthenticatedServerProcess()
    try:
      # assert that you can't fetch nothing without key
      try:
        requests.get(self.backend_https_auth_url, verify=False)
      except Exception:
        pass
      else:
        self.fail(
          'Access to %r shall be not possible without certificate' % (
            self.backend_https_auth_url,))
      # check that you can access this backend via frontend
      # (so it means that auth to backend worked)
      result = fakeHTTPSResult(
        parameter_dict['domain'],
        'test-path/deep/.././deeper',
        headers={
          'Timeout': '10',  # more than default backend-connect-timeout == 5
          'Accept-Encoding': 'gzip',
        }
      )

      self.assertEqual(
        self.certificate_pem,
        der2pem(result.peercert))

      self.assertEqual(
        result.status_code,
        httplib.BAD_GATEWAY
      )
    finally:
      self.stopAuthenticatedServerProcess()

  def test_auth_to_backend_backend_ignore(self):
    parameter_dict = self.assertSlaveBase('auth-to-backend-backend-ignore')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
      }
    )

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))

    self.assertEqual(j['Incoming Headers']['timeout'], '10')
    self.assertFalse('Content-Encoding' in result.headers)
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqual(
      'secured=value;secure, nonsecured=value',
      result.headers['Set-Cookie']
    )

    result_http = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://authtobackendbackendignore.example.com:%s/test-path/deeper' % (
        HTTP_PORT,),
      result_http.headers['Location']
    )

  def test_compressed_result(self):
    reference = 'Url'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = reference.translate(None, '_-').lower()
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          "slave url ' %s/?a=b&c= ' has been converted to '%s/?a=b&c='" % (
            self.backend_url, self.backend_url)],
      },
      parameter_dict
    )

    result_compressed = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Accept-Encoding': 'gzip',
        'Compress': '1',
      }
    )
    self.assertEqual(
      'gzip',
      result_compressed.headers['Content-Encoding']
    )

    # Assert that no tampering was done with the request
    # (compression/decompression)
    # Backend compresses with 0 level, so decompression/compression
    # would change somthing
    self.assertEqual(
      result_compressed.headers['Content-Length'],
      result_compressed.headers['Backend-Content-Length']
    )

    result_not_compressed = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Accept-Encoding': 'gzip',
      }
    )
    self.assertFalse('Content-Encoding' in result_not_compressed.headers)

  def test_no_content_type_alter(self):
    reference = 'Url'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = reference.translate(None, '_-').lower()
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          "slave url ' %s/?a=b&c= ' has been converted to '%s/?a=b&c='" % (
            self.backend_url, self.backend_url)],
      },
      parameter_dict
    )
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Accept-Encoding': 'gzip',
        'X-Reply-Body': base64.b64encode(
          b"""<?xml version="1.0" encoding="UTF-8"?>
<note>
  <to>Tove</to>
  <from>Jani</from>
  <heading>Reminder</heading>
  <body>Don't forget me this weekend!</body>
</note>"""),
        'X-Drop-Header': 'Content-Type'
      }
    )

    self.assertEqual(
      'text/xml; charset=utf-8',
      result.headers['Content-Type']
    )

  @skip('Feature postponed')
  def test_url_ipv6_access(self):
    parameter_dict = self.parseSlaveParameterDict('url')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'url.example.com',
        'replication_number': '1',
        'url': 'http://url.example.com',
        'site_url': 'http://url.example.com',
        'secure_access': 'https://url.example.com',
      },
      parameter_dict
    )

    result_ipv6 = fakeHTTPSResult(
      parameter_dict['domain'], self._ipv6_address, 'test-path',
      source_ip=self._ipv6_address)

    self.assertEqual(
       self._ipv6_address,
       result_ipv6.json()['Incoming Headers']['x-forwarded-for']
    )

    self.assertEqual(
      self.certificate_pem,
      der2pem(result_ipv6.peercert))

    self.assertEqualResultJson(result_ipv6, 'Path', '/test-path')

  def test_type_zope_path(self):
    parameter_dict = self.assertSlaveBase('type-zope-path')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/'
      'https//typezopepath.example.com:443/path/to/some/resource'
      '/VirtualHostRoot/'
      'test-path/deeper'
    )

  def test_type_zope_default_path(self):
    parameter_dict = self.assertSlaveBase('type-zope-default-path')

    result = fakeHTTPSResult(
      parameter_dict['domain'], '')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.MOVED_PERMANENTLY,
      result.status_code
    )

    self.assertEqual(
      'https://typezopedefaultpath.example.com:%s/'
      'default-path/to/some/resource' % (
        HTTPS_PORT,),
      result.headers['Location']
    )

  def test_server_alias(self):
    parameter_dict = self.assertSlaveBase('server-alias')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200', result.headers['Strict-Transport-Security'])
    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPSResult(
      'alias1.example.com',
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200', result.headers['Strict-Transport-Security'])
    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPSResult(
      'alias2.example.com',
      'test-path/deep/.././deeper')

    self.assertEqual(
      'max-age=200', result.headers['Strict-Transport-Security'])
    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

  def test_server_alias_empty(self):
    parameter_dict = self.assertSlaveBase('server-alias-empty')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
      }
    )

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200; includeSubDomains',
      result.headers['Strict-Transport-Security'])
    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))

    self.assertEqual(j['Incoming Headers']['timeout'], '10')
    self.assertFalse('Content-Encoding' in result.headers)
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqual(
      'secured=value;secure, nonsecured=value',
      result.headers['Set-Cookie']
    )

  def test_server_alias_wildcard(self):
    parameter_dict = self.parseSlaveParameterDict('server-alias-wildcard')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'serveraliaswildcard.example.com',
        'replication_number': '1',
        'url': 'http://serveraliaswildcard.example.com',
        'site_url': 'http://serveraliaswildcard.example.com',
        'secure_access': 'https://serveraliaswildcard.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200; preload',
      result.headers['Strict-Transport-Security'])
    self.assertEqualResultJson(result, 'Path', '/test-path')

    result = fakeHTTPSResult(
      'wild.alias1.example.com', 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200; preload',
      result.headers['Strict-Transport-Security'])
    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_server_alias_duplicated(self):
    parameter_dict = self.parseSlaveParameterDict('server-alias-duplicated')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'serveraliasduplicated.example.com',
        'replication_number': '1',
        'url': 'http://serveraliasduplicated.example.com',
        'site_url': 'http://serveraliasduplicated.example.com',
        'secure_access': 'https://serveraliasduplicated.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    result = fakeHTTPSResult(
      'alias3.example.com', 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_server_alias_custom_domain_duplicated(self):
    parameter_dict = self.parseSlaveParameterDict(
      'server-alias_custom_domain-duplicated')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'alias4.example.com',
        'replication_number': '1',
        'url': 'http://alias4.example.com',
        'site_url': 'http://alias4.example.com',
        'secure_access': 'https://alias4.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  @skip('Feature postponed')
  def test_check_error_log(self):
    # Caddy: Need to implement similar thing like check-error-on-apache-log
    raise NotImplementedError(self.id())

  def test_ssl_ca_crt(self):
    parameter_dict = self.parseSlaveParameterDict(
      'custom_domain_ssl_crt_ssl_key_ssl_ca_crt')
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'customdomainsslcrtsslkeysslcacrt.example.com',
        'replication_number': '1',
        'url': 'http://customdomainsslcrtsslkeysslcacrt.example.com',
        'site_url': 'http://customdomainsslcrtsslkeysslcacrt.example.com',
        'secure_access':
        'https://customdomainsslcrtsslkeysslcacrt.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = self.customdomain_ca_certificate_pem + \
        self.customdomain_ca_key_pem + \
        self.ca.certificate_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.customdomain_ca_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'autocert',
      '_custom_domain_ssl_crt_ssl_key_ssl_ca_crt.pem'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      self.assertEqual(data, out.read())

  def test_ssl_ca_crt_only(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_ca_crt_only')
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslcacrtonly.example.com',
        'replication_number': '1',
        'url': 'http://sslcacrtonly.example.com',
        'site_url': 'http://sslcacrtonly.example.com',
        'secure_access':
        'https://sslcacrtonly.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )
    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = self.ca.certificate_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)

    self.assertEqual(httplib.UNPROCESSABLE_ENTITY, upload.status_code)
    self.assertEqual('Key incorrect', upload.text)

  def test_ssl_ca_crt_garbage(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_ca_crt_garbage')
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslcacrtgarbage.example.com',
        'replication_number': '1',
        'url': 'http://sslcacrtgarbage.example.com',
        'site_url': 'http://sslcacrtgarbage.example.com',
        'secure_access':
        'https://sslcacrtgarbage.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    _, ca_key_pem, csr, _ = createCSR(
      parameter_dict['domain'])
    _, ca_certificate_pem = self.ca.signCSR(csr)

    data = ca_certificate_pem + ca_key_pem + 'some garbage'
    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)

    self.assertEqual(httplib.CREATED, upload.status_code)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
        parameter_dict['domain'], 'test-path')

    self.assertEqual(
      ca_certificate_pem,
      der2pem(result.peercert)
    )

    self.assertEqualResultJson(result, 'Path', '/test-path')

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'autocert',
      '_ssl_ca_crt_garbage.pem'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      self.assertEqual(data, out.read())

  def test_ssl_ca_crt_does_not_match(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_ca_crt_does_not_match')
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslcacrtdoesnotmatch.example.com',
        'replication_number': '1',
        'url': 'http://sslcacrtdoesnotmatch.example.com',
        'site_url': 'http://sslcacrtdoesnotmatch.example.com',
        'secure_access':
        'https://sslcacrtdoesnotmatch.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )
    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = self.certificate_pem + self.key_pem + self.ca.certificate_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)

    self.assertEqual(httplib.CREATED, upload.status_code)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'autocert',
      '_ssl_ca_crt_does_not_match.pem'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      self.assertEqual(data, out.read())

  def test_https_only(self):
    parameter_dict = self.assertSlaveBase('https-only')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result_http = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(result_http, 'Path', '/test-path/deeper')

  def test_custom_domain(self):
    reference = 'custom_domain'
    hostname = 'mycustomdomain'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_custom_domain_server_alias(self):
    reference = 'custom_domain_server_alias'
    hostname = 'mycustomdomainserveralias'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    result = fakeHTTPSResult(
      'mycustomdomainserveralias1.example.com',
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

  def test_custom_domain_wildcard(self):
    parameter_dict = self.parseSlaveParameterDict('custom_domain_wildcard')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': '*.customdomain.example.com',
        'replication_number': '1',
        'url': 'http://*.customdomain.example.com',
        'site_url': 'http://*.customdomain.example.com',
        'secure_access': 'https://*.customdomain.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      'wild.customdomain.example.com',
      'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_custom_domain_ssl_crt_ssl_key(self):
    reference = 'custom_domain_ssl_crt_ssl_key'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)

    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)
    data = self.customdomain_certificate_pem + \
        self.customdomain_key_pem
    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.customdomain_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_type_zope(self):
    parameter_dict = self.assertSlaveBase('type-zope')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//typezope.example.com:443/'
      '/VirtualHostRoot/test-path/deeper'
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://typezope.example.com:%s/test-path/deep/.././deeper' % (
        HTTP_PORT,),
      result.headers['Location']
    )

  def test_type_zope_prefer_gzip_encoding_to_backend_https_only(self):
    parameter_dict = self.assertSlaveBase(
      'type-zope-prefer-gzip-encoding-to-backend-https-only')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//'
      'typezopeprefergzipencodingtobackendhttpsonly.example.com:443/'
      '/VirtualHostRoot/test-path/deeper'
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/http//'
      'typezopeprefergzipencodingtobackendhttpsonly.example.com:80/'
      '/VirtualHostRoot/test-path/deeper'
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//'
      'typezopeprefergzipencodingtobackendhttpsonly.example.com:443/'
      '/VirtualHostRoot/test-path/deeper'
    )
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/http//'
      'typezopeprefergzipencodingtobackendhttpsonly.example.com:80/'
      '/VirtualHostRoot/test-path/deeper'
    )
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

  def test_type_zope_prefer_gzip_encoding_to_backend(self):
    parameter_dict = self.assertSlaveBase(
      'type-zope-prefer-gzip-encoding-to-backend')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//'
      'typezopeprefergzipencodingtobackend.example.com:443/'
      '/VirtualHostRoot/test-path/deeper'
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deep/.././deeper' % (
        parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//'
      'typezopeprefergzipencodingtobackend.example.com:443/'
      '/VirtualHostRoot/test-path/deeper'
    )
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deep/.././deeper' % (
        parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

  def test_type_zope_virtualhostroot_http_port(self):
    parameter_dict = self.assertSlaveBase(
      'type-zope-virtualhostroot-http-port')

    result = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/http//typezopevirtualhostroothttpport'
      '.example.com:12345//VirtualHostRoot/test-path'
    )

  def test_type_zope_virtualhostroot_https_port(self):
    parameter_dict = self.assertSlaveBase(
      'type-zope-virtualhostroot-https-port')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/VirtualHostBase/https//typezopevirtualhostroothttpsport'
      '.example.com:12345//VirtualHostRoot/test-path'
    )

  def test_type_notebook(self):
    reference = 'type-notebook'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path',
      HTTPS_PORT)

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test/terminals/websocket/test',
      HTTPS_PORT)

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/terminals/websocket')
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_type_websocket(self):
    parameter_dict = self.assertSlaveBase(
      'type-websocket')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/test-path'
    )
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertTrue('x-real-ip' in j['Incoming Headers'])
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_type_websocket_websocket_transparent_false(self):
    parameter_dict = self.assertSlaveBase(
      'type-websocket-websocket-transparent-false')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/test-path'
    )
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    parsed = urlparse.urlparse(self.backend_url)
    self.assertBackendHeaders(
      j['Incoming Headers'], parsed.hostname, port='17', proto='irc',
      ignore_header_list=['Host'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertFalse('x-real-ip' in j['Incoming Headers'])
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_type_websocket_websocket_path_list(self):
    parameter_dict = self.assertSlaveBase(
      'type-websocket-websocket-path-list')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])
    self.assertTrue('x-real-ip' in j['Incoming Headers'])

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'ws/test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqualResultJson(
      result,
      'Path',
      '/ws/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertTrue('x-real-ip' in j['Incoming Headers'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'with%20space/test-path', headers={'Connection': 'Upgrade'})

    self.assertEqualResultJson(
      result,
      'Path',
      '/with%20space/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertTrue('x-real-ip' in j['Incoming Headers'])

  def test_type_websocket_websocket_path_list_websocket_transparent_false(
    self):
    parameter_dict = self.assertSlaveBase(
      'type-websocket-websocket-path-list-websocket-transparent-false')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(
      result,
      'Path',
      '/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    parsed = urlparse.urlparse(self.backend_url)
    self.assertBackendHeaders(
      j['Incoming Headers'], parsed.hostname, port='17', proto='irc',
      ignore_header_list=['Host'])
    self.assertFalse('x-real-ip' in j['Incoming Headers'])

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'ws/test-path',
      headers={'Connection': 'Upgrade'})

    self.assertEqualResultJson(
      result,
      'Path',
      '/ws/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(
      j['Incoming Headers'], parsed.hostname, port='17', proto='irc',
      ignore_header_list=['Host'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertFalse('x-real-ip' in j['Incoming Headers'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'with%20space/test-path', headers={'Connection': 'Upgrade'})

    self.assertEqualResultJson(
      result,
      'Path',
      '/with%20space/test-path'
    )
    self.assertFalse(
      isHTTP2(parameter_dict['domain']))
    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(
      j['Incoming Headers'], parsed.hostname, port='17', proto='irc',
      ignore_header_list=['Host'])
    self.assertEqual(
      'Upgrade',
      j['Incoming Headers']['connection']
    )
    self.assertFalse('x-real-ip' in j['Incoming Headers'])

  @skip('Feature postponed')
  def test_type_eventsource(self):
    # Caddy: For event source, if I understand
    #        https://github.com/mholt/caddy/issues/1355 correctly, we could use
    #        Caddy as a proxy in front of nginx-push-stream . If we have a
    #        "central shared" caddy instance, can it handle keeping connections
    #        opens for many clients ?
    parameter_dict = self.parseSlaveParameterDict('type-eventsource')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'typeeventsource.nginx.example.com',
        'replication_number': '1',
        'url': 'http://typeeventsource.nginx.example.com',
        'site_url': 'http://typeeventsource.nginx.example.com',
        'secure_access': 'https://typeeventsource.nginx.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'pub',
      #  NGINX_HTTPS_PORT
    )

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      '',
      result.content
    )
    headers = result.headers.copy()
    self.assertKeyWithPop('Expires', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertEqual(
      {
        'X-Nginx-PushStream-Explain': 'No channel id provided.',
        'Content-Length': '0',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Connection': 'keep-alive',
        'Server': 'nginx'
      },
      headers
    )

  def test_type_redirect(self):
    parameter_dict = self.assertSlaveBase('type-redirect')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      '%stest-path/deeper' % (self.backend_url,),
      result.headers['Location']
    )

  def test_type_redirect_custom_domain(self):
    reference = 'type-redirect-custom_domain'
    hostname = 'customdomaintyperedirect'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      '%stest-path/deeper' % (self.backend_url,),
      result.headers['Location']
    )

  def test_ssl_proxy_verify_ssl_proxy_ca_crt_unverified(self):
    parameter_dict = self.parseSlaveParameterDict(
      'ssl-proxy-verify_ssl_proxy_ca_crt-unverified')

    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslproxyverifysslproxycacrtunverified.example.com',
        'replication_number': '1',
        'url': 'http://sslproxyverifysslproxycacrtunverified.example.com',
        'site_url':
        'http://sslproxyverifysslproxycacrtunverified.example.com',
        'secure_access':
        'https://sslproxyverifysslproxycacrtunverified.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.SERVICE_UNAVAILABLE,
      result.status_code
    )

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://sslproxyverifysslproxycacrtunverified.example.com:%s/'
      'test-path' % (HTTP_PORT,),
      result_http.headers['Location']
    )

  def test_ssl_proxy_verify_ssl_proxy_ca_crt(self):
    parameter_dict = self.assertSlaveBase('ssl-proxy-verify_ssl_proxy_ca_crt')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertBackendHeaders(j['Incoming Headers'], parameter_dict['domain'])

    self.assertFalse('Content-Encoding' in result.headers)

    self.assertEqual(
      'secured=value;secure, nonsecured=value',
      result.headers['Set-Cookie']
    )

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://sslproxyverifysslproxycacrt.example.com:%s/test-path' % (
        HTTP_PORT,),
      result_http.headers['Location']
    )

  def test_ssl_proxy_verify_unverified(self):
    parameter_dict = self.assertSlaveBase('ssl-proxy-verify-unverified')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.SERVICE_UNAVAILABLE,
      result.status_code
    )

  def test_monitor_ipv6_test(self):
    parameter_dict = self.assertSlaveBase('monitor-ipv6-test')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://monitoripv6test.example.com:%s/test-path' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    monitor_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        'check-_monitor-ipv6-test-ipv6-packet-list-test.py'))[0]
    # get promise module and check that parameters are ok
    self.assertEqual(
      getPromisePluginParameterDict(monitor_file),
      {
        'frequency': '720',
        'address': 'monitor-ipv6-test'
      }
    )

  def test_monitor_ipv4_test(self):
    parameter_dict = self.assertSlaveBase('monitor-ipv4-test')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://monitoripv4test.example.com:%s/test-path' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    monitor_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        'check-_monitor-ipv4-test-ipv4-packet-list-test.py'))[0]
    # get promise module and check that parameters are ok
    self.assertEqual(
      getPromisePluginParameterDict(monitor_file),
      {
        'frequency': '720',
        'ipv4': 'true',
        'address': 'monitor-ipv4-test',
      }
    )

  def test_ciphers(self):
    parameter_dict = self.assertSlaveBase('ciphers')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertEqual(
      'https://ciphers.example.com:%s/test-path' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    configuration_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'caddy-slave-conf.d', '_ciphers.conf'
      ))[0]
    self.assertTrue(
      'ciphers RSA-3DES-EDE-CBC-SHA RSA-AES128-CBC-SHA'
      in open(configuration_file).read()
    )

  def test_enable_cache_custom_domain(self):
    reference = 'enable_cache_custom_domain'
    hostname = 'customdomainenablecache'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
        'Cache-Control': 'max-age=1, stale-while-revalidate=3600, '
                         'stale-if-error=3600'
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

  def test_enable_cache_server_alias(self):
    parameter_dict = self.assertSlaveBase('enable_cache_server_alias')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
        'Cache-Control': 'max-age=1, stale-while-revalidate=3600, '
                         'stale-if-error=3600'
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

    result = fakeHTTPResult(
      'enablecacheserveralias1.example.com',
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})
    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://enablecacheserveralias1.example.com:%s/test-path/deeper' % (
        HTTP_PORT,),
      result.headers['Location']
    )

  def test_enable_cache_https_only(self):
    parameter_dict = self.assertSlaveBase('enable_cache-https-only')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
        'Cache-Control': 'max-age=1, stale-while-revalidate=3600, '
                         'stale-if-error=3600'
      },
      headers
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'HTTPS/test', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqual(httplib.OK, result.status_code)
    self.assertEqualResultJson(result, 'Path', '/HTTPS/test')

    headers = result.headers.copy()

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'HTTP/test', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqual(httplib.OK, result.status_code)
    self.assertEqualResultJson(result, 'Path', '/HTTP/test')

    headers = result.headers.copy()

  def test_enable_cache(self):
    parameter_dict = self.assertSlaveBase('enable_cache')

    source_ip = '127.0.0.1'
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600',
      },
      source_ip=source_ip
    )

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
        'Cache-Control': 'max-age=1, stale-while-revalidate=3600, '
                         'stale-if-error=3600'
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

    # BEGIN: Check that squid.log is correctly filled in
    ats_log_file_list = glob.glob(
      os.path.join(
        self.instance_path, '*', 'var', 'log', 'trafficserver', 'squid.log'
      ))
    self.assertEqual(1, len(ats_log_file_list))
    ats_log_file = ats_log_file_list[0]
    direct_pattern = re.compile(
      r'.*TCP_MISS/200 .*test-path/deeper.*enablecache.example.com'
      '.* - DIRECT*')
    # ATS needs some time to flush logs
    timeout = 10
    b = time.time()
    while True:
      direct_pattern_match = 0
      if (time.time() - b) > timeout:
        break
      with open(ats_log_file) as fh:
        for line in fh.readlines():
          if direct_pattern.match(line):
            direct_pattern_match += 1
      if direct_pattern_match > 0:
        break
      time.sleep(0.1)

    with open(ats_log_file) as fh:
      ats_log = fh.read()
    self.assertRegexpMatches(ats_log, direct_pattern)
    # END: Check that squid.log is correctly filled in

  def _hack_ats(self, max_stale_age):
    records_config = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'trafficserver', 'records.config'
      ))
    self.assertEqual(1, len(records_config))
    self._hack_ats_records_config_path = records_config[0]
    original_max_stale_age = \
        'CONFIG proxy.config.http.cache.max_stale_age INT 604800\n'
    new_max_stale_age = \
        'CONFIG proxy.config.http.cache.max_stale_age INT %s\n' % (
          max_stale_age,)
    with open(self._hack_ats_records_config_path) as fh:
      self._hack_ats_original_records_config = fh.readlines()
    # sanity check - are we really do it?
    self.assertIn(
      original_max_stale_age,
      self._hack_ats_original_records_config)
    new_records_config = []
    max_stale_age_changed = False
    for line in self._hack_ats_original_records_config:
      if line == original_max_stale_age:
        line = new_max_stale_age
        max_stale_age_changed = True
      new_records_config.append(line)
    self.assertTrue(max_stale_age_changed)
    with open(self._hack_ats_records_config_path, 'w') as fh:
      fh.write(''.join(new_records_config))
    self._hack_ats_restart()

  def _unhack_ats(self):
    with open(self._hack_ats_records_config_path, 'w') as fh:
      fh.write(''.join(self._hack_ats_original_records_config))
    self._hack_ats_restart()

  def _hack_ats_restart(self):
    for process_info in self.callSupervisorMethod('getAllProcessInfo'):
      if process_info['name'].startswith(
        'trafficserver') and process_info['name'].endswith('-on-watch'):
        self.callSupervisorMethod(
          'stopProcess', '%(group)s:%(name)s' % process_info)
        self.callSupervisorMethod(
          'startProcess', '%(group)s:%(name)s' % process_info)
    # give short time for the ATS to start back
    time.sleep(5)
    for process_info in self.callSupervisorMethod('getAllProcessInfo'):
      if process_info['name'].startswith(
        'trafficserver') and process_info['name'].endswith('-on-watch'):
        self.assertEqual(process_info['statename'], 'RUNNING')

  def test_enable_cache_negative_revalidate(self):
    parameter_dict = self.assertSlaveBase('enable_cache')

    source_ip = '127.0.0.1'
    # have unique path for this test
    path = self.id()

    max_stale_age = 30
    max_age = int(max_stale_age / 2.)
    # body_200 is big enough to trigger
    # https://github.com/apache/trafficserver/issues/7880
    body_200 = b'Body 200' * 500
    body_502 = b'Body 502'
    body_502_new = b'Body 502 new'
    body_200_new = b'Body 200 new'

    self.addCleanup(self._unhack_ats)
    self._hack_ats(max_stale_age)

    def configureResult(status_code, body):
      backend_url = self.getSlaveParameterDictDict()['enable_cache']['url']
      result = requests.put(backend_url + path, headers={
          'X-Reply-Header-Cache-Control': 'max-age=%s, public' % (max_age,),
          'X-Reply-Status-Code': status_code,
          'X-Reply-Body': base64.b64encode(body),
          # drop Content-Length header to ensure
          # https://github.com/apache/trafficserver/issues/7880
          'X-Drop-Header': 'Content-Length',
        })
      self.assertEqual(result.status_code, httplib.CREATED)

    def checkResult(status_code, body):
      result = fakeHTTPSResult(
        parameter_dict['domain'], path,
        source_ip=source_ip
      )
      self.assertEqual(result.status_code, status_code)
      self.assertEqual(result.text, body)

    # backend returns something correctly
    configureResult('200', body_200)
    checkResult(httplib.OK, body_200)

    configureResult('502', body_502)
    time.sleep(1)
    # even if backend returns 502, ATS gives cached result
    checkResult(httplib.OK, body_200)

    # interesting moment, time is between max_age and max_stale_age, triggers
    # https://github.com/apache/trafficserver/issues/7880
    time.sleep(max_age + 1)
    checkResult(httplib.OK, body_200)

    # max_stale_age passed, time to return 502 from the backend
    time.sleep(max_stale_age + 2)
    checkResult(httplib.BAD_GATEWAY, body_502)

    configureResult('502', body_502_new)
    time.sleep(1)
    # even if there is new negative response on the backend, the old one is
    # served from the cache
    checkResult(httplib.BAD_GATEWAY, body_502)

    time.sleep(max_age + 2)
    # now as max-age of negative response passed, the new one is served
    checkResult(httplib.BAD_GATEWAY, body_502_new)

    configureResult('200', body_200_new)
    time.sleep(1)
    checkResult(httplib.BAD_GATEWAY, body_502_new)
    time.sleep(max_age + 2)
    # backend is back to normal, as soon as negative response max-age passed
    # the new response is served
    checkResult(httplib.OK, body_200_new)

  @skip('Feature postponed')
  def test_enable_cache_stale_if_error_respected(self):
    parameter_dict = self.assertSlaveBase('enable_cache')

    source_ip = '127.0.0.1'
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600',
      },
      source_ip=source_ip
    )

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
        'Cache-Control': 'max-age=1, stale-while-revalidate=3600, '
                         'stale-if-error=3600'
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

    # check stale-if-error support is really respected if not present in the
    # request
    # wait a bit for max-age to expire
    time.sleep(2)
    # real check: cache access does not provide old data with stopped backend
    try:
      # stop the backend, to have error on while connecting to it
      self.stopServerProcess()

      result = fakeHTTPSResult(
        parameter_dict['domain'],
        'test-path/deep/.././deeper', headers={
          'X-Reply-Header-Cache-Control': 'max-age=1',
        },
        source_ip=source_ip
      )
      self.assertEqual(result.status_code, httplib.BAD_GATEWAY)
    finally:
      self.startServerProcess()
    # END: check stale-if-error support

  def test_enable_cache_ats_timeout(self):
    parameter_dict = self.assertSlaveBase('enable_cache')
    # check that timeout seen by ATS does not result in many queries done
    # to the backend and that next request works like a charm
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test_enable_cache_ats_timeout', headers={
        'Timeout': '15',
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    # ATS timed out
    self.assertEqual(
      httplib.GATEWAY_TIMEOUT,
      result.status_code
    )

    backend_haproxy_log_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'var', 'log', 'backend-haproxy.log'
      ))[0]

    matching_line_amount = 0
    pattern = re.compile(
      r'.* _enable_cache-http.backend .* 504 .*'
      '"GET .test_enable_cache_ats_timeout HTTP.1.1"$')
    with open(backend_haproxy_log_file) as fh:
      for line in fh.readlines():
        if pattern.match(line):
          matching_line_amount += 1

    # Haproxy backend received maximum one connection
    self.assertIn(matching_line_amount, [0, 1])

    timeout = 5
    b = time.time()
    # ATS created squid.log with a delay
    while True:
      if (time.time() - b) > timeout:
        self.fail('Squid log file did not appear in %ss' % (timeout,))
      ats_log_file_list = glob.glob(
        os.path.join(
          self.instance_path, '*', 'var', 'log', 'trafficserver', 'squid.log'
        ))
      if len(ats_log_file_list) == 1:
        ats_log_file = ats_log_file_list[0]
        break
      time.sleep(0.1)

    pattern = re.compile(
      r'.*ERR_READ_TIMEOUT/504 .*test_enable_cache_ats_timeout'
      '.*TIMEOUT_DIRECT*')
    timeout = 10
    b = time.time()
    # ATS needs some time to flush logs
    while True:
      matching_line_amount = 0
      if (time.time() - b) > timeout:
        break
      with open(ats_log_file) as fh:
        for line in fh.readlines():
          if pattern.match(line):
            matching_line_amount += 1
      if matching_line_amount > 0:
        break
      time.sleep(0.1)

    # ATS has maximum one entry for this query
    self.assertIn(matching_line_amount, [0, 1])

    # the result is available immediately after
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper', headers={
        'X-Reply-Header-Cache-Control': 'max-age=1, stale-while-'
        'revalidate=3600, stale-if-error=3600'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

  def test_enable_cache_disable_no_cache_request(self):
    parameter_dict = self.assertSlaveBase(
      'enable_cache-disable-no-cache-request')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      headers={'Pragma': 'no-cache', 'Cache-Control': 'something'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value'
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

    try:
      j = result.json()
    except Exception:
      raise ValueError('JSON decode problem in:\n%s' % (result.text,))
    self.assertFalse('pragma' in j['Incoming Headers'].keys())

  def test_enable_cache_disable_via_header(self):
    parameter_dict = self.assertSlaveBase('enable_cache-disable-via-header')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)
    self.assertKeyWithPop('Age', headers)

    # drop keys appearing randomly in headers
    headers.pop('Transfer-Encoding', None)
    headers.pop('Content-Length', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
      },
      headers
    )

    backend_headers = result.json()['Incoming Headers']
    self.assertBackendHeaders(backend_headers, parameter_dict['domain'])
    via = backend_headers.pop('via', None)
    self.assertNotEqual(via, None)
    self.assertRegexpMatches(
      via,
      r'^http\/1.1 caddy-frontend-1\[.*\] \(ApacheTrafficServer\/9.0.1\)$'
    )

  def test_enable_http2_false(self):
    parameter_dict = self.assertSlaveBase('enable-http2-false')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)

    # drop vary-keys
    headers.pop('Content-Length', None)
    headers.pop('Transfer-Encoding', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-Type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
      },
      headers
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_default(self):
    parameter_dict = self.assertSlaveBase('enable-http2-default')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)

    # drop vary-keys
    headers.pop('Content-Length', None)
    headers.pop('Transfer-Encoding', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
      },
      headers
    )

    self.assertTrue(
      isHTTP2(parameter_dict['domain']))

  def test_prefer_gzip_encoding_to_backend_https_only(self):
    parameter_dict = self.assertSlaveBase(
      'prefer-gzip-encoding-to-backend-https-only')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'deflate'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'deflate', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'],
      port=HTTP_PORT, proto='http')
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'deflate'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'],
      port=HTTP_PORT, proto='http')
    self.assertEqual(
      'deflate', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

  def test_prefer_gzip_encoding_to_backend(self):
    parameter_dict = self.assertSlaveBase(
      'prefer-gzip-encoding-to-backend')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'gzip', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'deflate'})

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'deflate', result.json()['Incoming Headers']['accept-encoding'])

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'gzip, deflate'})

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deeper' % (parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={'Accept-Encoding': 'deflate'})

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deeper' % (parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deeper' % (parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

    result = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result.status_code
    )

    self.assertEqual(
      'https://%s:%s/test-path/deeper' % (parameter_dict['domain'], HTTP_PORT),
      result.headers['Location']
    )

  def test_disabled_cookie_list(self):
    parameter_dict = self.assertSlaveBase('disabled-cookie-list')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      cookies=dict(
          Chocolate='absent',
          Vanilia='absent',
          Coffee='present'
        ))

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    self.assertBackendHeaders(
      result.json()['Incoming Headers'], parameter_dict['domain'])
    self.assertEqual(
      'Coffee=present', result.json()['Incoming Headers']['cookie'])

  def test_https_url(self):
    parameter_dict = self.assertSlaveBase('url_https-url')

    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      'max-age=200; includeSubDomains; preload',
      result.headers['Strict-Transport-Security'])

    self.assertEqualResultJson(result, 'Path', '/https/test-path/deeper')

    result_http = fakeHTTPResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper')

    self.assertEqual(
      httplib.FOUND,
      result_http.status_code
    )

    self.assertNotIn('Strict-Transport-Security', result_http.headers)

    self.assertEqual(
      'https://urlhttpsurl.example.com:%s/test-path/deeper' % (HTTP_PORT,),
      result_http.headers['Location']
    )

    # check that timeouts are correctly set in the haproxy configuration
    backend_configuration_file = glob.glob(os.path.join(
      self.instance_path, '*', 'etc', 'backend-haproxy.cfg'))[0]
    with open(backend_configuration_file) as fh:
      content = fh.read()
      self.assertTrue("""backend _url_https-url-http
  timeout server 15s
  timeout connect 10s
  retries 5""" in content)


class TestReplicateSlave(SlaveHttpFrontendTestCase, TestDataMixin):
  instance_parameter_dict = {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getInstanceParameterDict(cls):
    return cls.instance_parameter_dict

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'replicate': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
    }

  def test(self):
    # now instantiate 2nd partition in started state
    # and due to port collision, stop the first one...
    self.instance_parameter_dict.update({
      '-frontend-quantity': 2,
      '-sla-2-computer_guid': self.slap._computer_id,
      '-frontend-1-state': 'stopped',
      '-frontend-2-state': 'started',
    })
    self.requestDefaultInstance()
    self.requestSlaves()
    self.slap.waitForInstance(self.instance_max_retry)
    # ...and be nice, put back the first one online
    self.instance_parameter_dict.update({
      '-frontend-1-state': 'started',
      '-frontend-2-state': 'stopped',
    })
    self.requestDefaultInstance()
    self.slap.waitForInstance(self.instance_max_retry)
    self.slap.waitForInstance(self.instance_max_retry)
    self.slap.waitForInstance(self.instance_max_retry)

    self.updateSlaveConnectionParameterDictDict()
    # the real assertions follow...
    parameter_dict = self.parseSlaveParameterDict('replicate')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'replicate.example.com',
        'replication_number': '2',
        'url': 'http://replicate.example.com',
        'site_url': 'http://replicate.example.com',
        'secure_access': 'https://replicate.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(httplib.FOUND, result_http.status_code)

    # prove 2nd frontend by inspection of the instance
    slave_configuration_name = '_replicate.conf'
    slave_configuration_file_list = [
      '/'.join([f[0], slave_configuration_name]) for f in [
        q for q in os.walk(self.instance_path)
        if slave_configuration_name in q[2]
      ]
    ]

    self.assertEqual(
      2, len(slave_configuration_file_list), slave_configuration_file_list)


class TestReplicateSlaveOtherDestroyed(SlaveHttpFrontendTestCase):
  instance_parameter_dict = {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getInstanceParameterDict(cls):
    return cls.instance_parameter_dict

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'empty': {
        'url': cls.backend_url,
        'enable_cache': True,
      }
    }

  def test_extra_slave_instance_list_not_present_destroyed_request(self):
    # now instantiate 2nd partition in started state
    # and due to port collision, stop the first one
    self.instance_parameter_dict.update({
      '-frontend-quantity': 2,
      '-sla-2-computer_guid': self.slap._computer_id,
      '-frontend-1-state': 'stopped',
      '-frontend-2-state': 'started',

    })
    self.requestDefaultInstance()
    self.slap.waitForInstance(self.instance_max_retry)

    # now start back first instance, and destroy 2nd one
    self.instance_parameter_dict.update({
      '-frontend-1-state': 'started',
      '-frontend-2-state': 'destroyed',
    })
    self.requestDefaultInstance()
    self.slap.waitForInstance(self.instance_max_retry)
    self.slap.waitForInstance(self.instance_max_retry)
    self.slap.waitForInstance(self.instance_max_retry)

    buildout_file = os.path.join(
      self.getMasterPartitionPath(), 'buildout-switch-softwaretype.cfg')
    with open(buildout_file) as fh:
      buildout_file_content = fh.read()
      node_1_present = re.search(
        "^config-frontend-name = !py!'caddy-frontend-1'$",
        buildout_file_content, flags=re.M) is not None
      node_2_present = re.search(
        "^config-frontend-name = !py!'caddy-frontend-2'$",
        buildout_file_content, flags=re.M) is not None
    self.assertTrue(node_1_present)
    self.assertFalse(node_2_present)


class TestEnableHttp2ByDefaultFalseSlave(SlaveHttpFrontendTestCase,
                                         TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'enable-http2-by-default': 'false',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'enable-http2-default': {
      },
      'enable-http2-false': {
        'enable-http2': 'false',
      },
      'enable-http2-true': {
        'enable-http2': 'true',
      },
      'dummy-cached': {
        'url': cls.backend_url,
        'enable_cache': True,
      }
    }

  def test_enable_http2_default(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2default.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2default.example.com',
        'site_url': 'http://enablehttp2default.example.com',
        'secure_access':
        'https://enablehttp2default.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_false(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-false')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2false.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2false.example.com',
        'site_url': 'http://enablehttp2false.example.com',
        'secure_access':
        'https://enablehttp2false.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_true(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-true')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2true.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2true.example.com',
        'site_url': 'http://enablehttp2true.example.com',
        'secure_access':
        'https://enablehttp2true.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertTrue(
      isHTTP2(parameter_dict['domain']))


class TestEnableHttp2ByDefaultDefaultSlave(SlaveHttpFrontendTestCase,
                                           TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'enable-http2-default': {
      },
      'enable-http2-false': {
        'enable-http2': 'false',
      },
      'enable-http2-true': {
        'enable-http2': 'true',
      },
      'dummy-cached': {
        'url': cls.backend_url,
        'enable_cache': True,
      }
    }

  def test_enable_http2_default(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2default.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2default.example.com',
        'site_url': 'http://enablehttp2default.example.com',
        'secure_access':
        'https://enablehttp2default.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertTrue(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_false(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-false')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2false.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2false.example.com',
        'site_url': 'http://enablehttp2false.example.com',
        'secure_access':
        'https://enablehttp2false.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_true(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-true')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2true.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'replication_number': '1',
        'url': 'http://enablehttp2true.example.com',
        'site_url': 'http://enablehttp2true.example.com',
        'secure_access':
        'https://enablehttp2true.example.com',
      },
      parameter_dict
    )

    self.assertTrue(
      isHTTP2(parameter_dict['domain']))


class TestRe6stVerificationUrlDefaultSlave(SlaveHttpFrontendTestCase,
                                           TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'default': {
        'url': cls.backend_url,
        'enable_cache': True
      },
    }

  @classmethod
  def waitForSlave(cls):
    # no need to wait for slave availability here
    return True

  def test_default(self):
    parameter_dict = self.parseSlaveParameterDict('default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'default.None',
        'replication_number': '1',
        'url': 'http://default.None',
        'site_url': 'http://default.None',
        'secure_access': 'https://default.None',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    re6st_connectivity_promise_list = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        're6st-connectivity.py'))

    self.assertEqual(1, len(re6st_connectivity_promise_list))
    re6st_connectivity_promise_file = re6st_connectivity_promise_list[0]

    self.assertEqual(
      getPromisePluginParameterDict(re6st_connectivity_promise_file),
      {
        'url': 'http://[2001:67c:1254:4::1]/index.html',
      }
    )


class TestRe6stVerificationUrlSlave(SlaveHttpFrontendTestCase,
                                    TestDataMixin):
  instance_parameter_dict = {
      'port': HTTPS_PORT,
      'domain': 'example.com',
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def getInstanceParameterDict(cls):
    return cls.instance_parameter_dict

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'default': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
    }

  def test_default(self):
    self.instance_parameter_dict[
      're6st-verification-url'] = 'some-re6st-verification-url'
    # re-request instance with updated parameters
    self.requestDefaultInstance()

    # run once instance, it's only needed for later checks
    try:
      self.slap.waitForInstance()
    except Exception:
      pass

    parameter_dict = self.parseSlaveParameterDict('default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'domain': 'default.example.com',
        'replication_number': '1',
        'url': 'http://default.example.com',
        'site_url': 'http://default.example.com',
        'secure_access': 'https://default.example.com',
      },
      parameter_dict
    )

    re6st_connectivity_promise_list = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        're6st-connectivity.py'))

    self.assertEqual(1, len(re6st_connectivity_promise_list))
    re6st_connectivity_promise_file = re6st_connectivity_promise_list[0]

    self.assertEqual(
      getPromisePluginParameterDict(re6st_connectivity_promise_file),
      {
        'url': 'some-re6st-verification-url',
      }
    )


class TestSlaveGlobalDisableHttp2(TestSlave):
  @classmethod
  def getInstanceParameterDict(cls):
    instance_parameter_dict = super(
      TestSlaveGlobalDisableHttp2, cls).getInstanceParameterDict()
    instance_parameter_dict['global-disable-http2'] = 'TrUe'
    return instance_parameter_dict

  def test_enable_http2_default(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2default.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2default.example.com',
        'site_url': 'http://enablehttp2default.example.com',
        'secure_access':
        'https://enablehttp2default.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    headers = result.headers.copy()

    self.assertKeyWithPop('Server', headers)
    self.assertKeyWithPop('Date', headers)

    # drop vary-keys
    headers.pop('Content-Length', None)
    headers.pop('Transfer-Encoding', None)
    headers.pop('Connection', None)
    headers.pop('Keep-Alive', None)

    self.assertEqual(
      {
        'Content-type': 'application/json',
        'Set-Cookie': 'secured=value;secure, nonsecured=value',
      },
      headers
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))


class TestEnableHttp2ByDefaultFalseSlaveGlobalDisableHttp2(
  TestEnableHttp2ByDefaultFalseSlave):
  @classmethod
  def getInstanceParameterDict(cls):
    instance_parameter_dict = super(
      TestEnableHttp2ByDefaultFalseSlaveGlobalDisableHttp2,
      cls).getInstanceParameterDict()
    instance_parameter_dict['global-disable-http2'] = 'TrUe'
    return instance_parameter_dict

  def test_enable_http2_true(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-true')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2true.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2true.example.com',
        'site_url': 'http://enablehttp2true.example.com',
        'secure_access':
        'https://enablehttp2true.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))


class TestEnableHttp2ByDefaultDefaultSlaveGlobalDisableHttp2(
  TestEnableHttp2ByDefaultDefaultSlave):
  @classmethod
  def getInstanceParameterDict(cls):
    instance_parameter_dict = super(
      TestEnableHttp2ByDefaultDefaultSlaveGlobalDisableHttp2,
      cls).getInstanceParameterDict()
    instance_parameter_dict['global-disable-http2'] = 'TrUe'
    return instance_parameter_dict

  def test_enable_http2_true(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-true')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2true.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2true.example.com',
        'site_url': 'http://enablehttp2true.example.com',
        'secure_access':
        'https://enablehttp2true.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))

  def test_enable_http2_default(self):
    parameter_dict = self.parseSlaveParameterDict('enable-http2-default')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'enablehttp2default.example.com',
        'replication_number': '1',
        'url': 'http://enablehttp2default.example.com',
        'site_url': 'http://enablehttp2default.example.com',
        'secure_access':
        'https://enablehttp2default.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    self.assertFalse(
      isHTTP2(parameter_dict['domain']))


class TestSlaveSlapOSMasterCertificateCompatibilityOverrideMaster(
  SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def setUpMaster(cls):
    # run partition until AIKC finishes
    cls.runComputerPartitionUntil(
      cls.untilNotReadyYetNotInMasterKeyGenerateAuthUrl)

    parameter_dict = cls.requestDefaultInstance().getConnectionParameterDict()
    ca_certificate = requests.get(
      parameter_dict['kedifa-caucase-url'] + '/cas/crt/ca.crt.pem')
    assert ca_certificate.status_code == httplib.OK
    cls.ca_certificate_file = os.path.join(cls.working_directory, 'ca.crt.pem')
    open(cls.ca_certificate_file, 'w').write(ca_certificate.text)
    # Do not upload certificates for the master partition

  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'apache-certificate': cls.certificate_pem,
      'apache-key': cls.key_pem,
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'ssl_from_master_kedifa_overrides_master_certificate': {
        'url': cls.backend_url,
        'enable_cache': True
      },
    }

  def test_ssl_from_master_kedifa_overrides_master_certificate(self):
    reference = 'ssl_from_master_kedifa_overrides_master_certificate'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = \
        createSelfSignedCertificate([parameter_dict['domain']])

    master_parameter_dict = \
        self.requestDefaultInstance().getConnectionParameterDict()
    auth = requests.get(
      master_parameter_dict['master-key-generate-auth-url'],
      verify=self.ca_certificate_file)
    requests.put(
      master_parameter_dict['master-key-upload-url'] + auth.text,
      data=key_pem + certificate_pem,
      verify=self.ca_certificate_file)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')


class TestSlaveSlapOSMasterCertificateCompatibility(
  SlaveHttpFrontendTestCase, TestDataMixin):

  @classmethod
  def setUpMaster(cls):
    # run partition until AIKC finishes
    cls.runComputerPartitionUntil(
      cls.untilNotReadyYetNotInMasterKeyGenerateAuthUrl)

    parameter_dict = cls.requestDefaultInstance().getConnectionParameterDict()
    ca_certificate = requests.get(
      parameter_dict['kedifa-caucase-url'] + '/cas/crt/ca.crt.pem')
    assert ca_certificate.status_code == httplib.OK
    cls.ca_certificate_file = os.path.join(cls.working_directory, 'ca.crt.pem')
    open(cls.ca_certificate_file, 'w').write(ca_certificate.text)
    # Do not upload certificates for the master partition

  @classmethod
  def prepareCertificate(cls):
    _, cls.ssl_from_slave_key_pem, _, cls.ssl_from_slave_certificate_pem = \
      createSelfSignedCertificate(
        [
          'sslfromslave.example.com',
        ])
    _, cls.ssl_from_slave_kedifa_overrides_key_pem, _, \
        cls.ssl_from_slave_kedifa_overrides_certificate_pem = \
        createSelfSignedCertificate(
          [
            'sslfromslavekedifaoverrides.example.com',
          ])
    _, cls.type_notebook_ssl_from_slave_key_pem, _, \
        cls.type_notebook_ssl_from_slave_certificate_pem = \
        createSelfSignedCertificate(
          [
            'typenotebooksslfromslave.example.com',
          ])
    _, cls.type_notebook_ssl_from_slave_kedifa_overrides_key_pem, _, \
        cls.type_notebook_ssl_from_slave_kedifa_overrides_certificate_pem = \
        createSelfSignedCertificate(
          [
            'typenotebooksslfromslavekedifaoverrides.example.com',
          ])

    cls.ca = CertificateAuthority(
      'TestSlaveSlapOSMasterCertificateCompatibility')

    _, cls.customdomain_ca_key_pem, csr, _ = createCSR(
      'customdomainsslcrtsslkeysslcacrt.example.com')
    _, cls.customdomain_ca_certificate_pem = cls.ca.signCSR(csr)

    _, cls.sslcacrtgarbage_ca_key_pem, csr, _ = createCSR(
      'sslcacrtgarbage.example.com')
    _, cls.sslcacrtgarbage_ca_certificate_pem = cls.ca.signCSR(csr)

    _, cls.ssl_from_slave_ca_key_pem, csr, _ = createCSR(
      'sslfromslave.example.com')
    _, cls.ssl_from_slave_ca_certificate_pem = cls.ca.signCSR(csr)

    _, cls.customdomain_key_pem, _, cls.customdomain_certificate_pem = \
        createSelfSignedCertificate(['customdomainsslcrtsslkey.example.com'])

    super(
      TestSlaveSlapOSMasterCertificateCompatibility, cls).prepareCertificate()

  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'apache-certificate': cls.certificate_pem,
      'apache-key': cls.key_pem,
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'ssl_from_master': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
      'ssl_from_master_kedifa_overrides': {
        'url': cls.backend_url,
      },
      'ssl_from_slave': {
        'url': cls.backend_url,
        'ssl_crt': cls.ssl_from_slave_certificate_pem,
        'ssl_key': cls.ssl_from_slave_key_pem,
      },
      'ssl_from_slave_kedifa_overrides': {
        'url': cls.backend_url,
        'ssl_crt': cls.ssl_from_slave_kedifa_overrides_certificate_pem,
        'ssl_key': cls.ssl_from_slave_kedifa_overrides_key_pem,
      },
      'custom_domain_ssl_crt_ssl_key': {
        'url': cls.backend_url,
        'ssl_crt': cls.customdomain_certificate_pem,
        'ssl_key': cls.customdomain_key_pem,
        'custom_domain': 'customdomainsslcrtsslkey.example.com'
      },
      'custom_domain_ssl_crt_ssl_key_ssl_ca_crt': {
        'url': cls.backend_url,
        'ssl_crt': cls.customdomain_ca_certificate_pem,
        'ssl_key': cls.customdomain_ca_key_pem,
        'ssl_ca_crt': cls.ca.certificate_pem,
        'custom_domain': 'customdomainsslcrtsslkeysslcacrt.example.com',
      },
      'ssl_ca_crt_garbage': {
        'url': cls.backend_url,
        'ssl_crt': cls.sslcacrtgarbage_ca_certificate_pem,
        'ssl_key': cls.sslcacrtgarbage_ca_key_pem,
        'ssl_ca_crt': 'some garbage',
      },
      'ssl_ca_crt_does_not_match': {
        'url': cls.backend_url,
        'ssl_crt': cls.certificate_pem,
        'ssl_key': cls.key_pem,
        'ssl_ca_crt': cls.ca.certificate_pem,
      },
      'type-notebook-ssl_from_master': {
        'url': cls.backend_url,
        'type': 'notebook',
      },
      'type-notebook-ssl_from_slave': {
        'url': cls.backend_url,
        'ssl_crt': cls.type_notebook_ssl_from_slave_certificate_pem,
        'ssl_key': cls.type_notebook_ssl_from_slave_key_pem,
        'type': 'notebook',
      },
      'type-notebook-ssl_from_master_kedifa_overrides': {
        'url': cls.backend_url,
        'type': 'notebook',
      },
      'type-notebook-ssl_from_slave_kedifa_overrides': {
        'url': cls.backend_url,
        'ssl_crt':
        cls.type_notebook_ssl_from_slave_kedifa_overrides_certificate_pem,
        'ssl_key':
        cls.type_notebook_ssl_from_slave_kedifa_overrides_key_pem,
        'type': 'notebook',
      }
    }

  def test_master_partition_state(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    expected_parameter_dict = {
      'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
      'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      'domain': 'example.com',
      'accepted-slave-amount': '12',
      'rejected-slave-amount': '0',
      'slave-amount': '12',
      'rejected-slave-dict': {
        # u"_ssl_ca_crt_only":
        # [u"ssl_ca_crt is present, so ssl_crt and ssl_key are required"],
        # u"_ssl_key-ssl_crt-unsafe":
        # [u"slave ssl_key and ssl_crt does not match"]
      },
      'warning-list': [
        u'apache-certificate is obsolete, please use master-key-upload-url',
        u'apache-key is obsolete, please use master-key-upload-url',
      ],
      'warning-slave-dict': {
        u'_custom_domain_ssl_crt_ssl_key': [
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url'
        ],
        u'_custom_domain_ssl_crt_ssl_key_ssl_ca_crt': [
          u'ssl_ca_crt is obsolete, please use key-upload-url',
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url'
        ],
        u'_ssl_ca_crt_does_not_match': [
          u'ssl_ca_crt is obsolete, please use key-upload-url',
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
        u'_ssl_ca_crt_garbage': [
          u'ssl_ca_crt is obsolete, please use key-upload-url',
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
        # u'_ssl_ca_crt_only': [
        #   u'ssl_ca_crt is obsolete, please use key-upload-url',
        # ],
        u'_ssl_from_slave': [
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
        u'_ssl_from_slave_kedifa_overrides': [
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
        # u'_ssl_key-ssl_crt-unsafe': [
        #   u'ssl_key is obsolete, please use key-upload-url',
        #   u'ssl_crt is obsolete, please use key-upload-url',
        # ],
        u'_type-notebook-ssl_from_slave': [
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
        u'_type-notebook-ssl_from_slave_kedifa_overrides': [
          u'ssl_crt is obsolete, please use key-upload-url',
          u'ssl_key is obsolete, please use key-upload-url',
        ],
      }
    }

    self.assertEqual(
      expected_parameter_dict,
      parameter_dict
    )

  def test_ssl_from_master(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_from_master')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = 'ssl_from_master'.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_ssl_from_master_kedifa_overrides(self):
    reference = 'ssl_from_master_kedifa_overrides'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = \
        createSelfSignedCertificate([parameter_dict['domain']])

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = certificate_pem + key_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_ssl_from_slave(self):
    reference = 'ssl_from_slave'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)

    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url',
         ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.ssl_from_slave_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_ssl_from_slave_kedifa_overrides(self):
    reference = 'ssl_from_slave_kedifa_overrides'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)

    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url',
         ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.ssl_from_slave_kedifa_overrides_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = \
        createSelfSignedCertificate([parameter_dict['domain']])

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = certificate_pem + key_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)

    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_type_notebook_ssl_from_master(self):
    reference = 'type-notebook-ssl_from_master'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_type_notebook_ssl_from_master_kedifa_overrides(self):
    reference = 'type-notebook-ssl_from_master_kedifa_overrides'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = \
        createSelfSignedCertificate([parameter_dict['domain']])

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = certificate_pem + key_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)

    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_type_notebook_ssl_from_slave(self):
    reference = 'type-notebook-ssl_from_slave'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url',
         ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      self.type_notebook_ssl_from_slave_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_type_notebook_ssl_from_slave_kedifa_overrides(self):
    reference = 'type-notebook-ssl_from_slave_kedifa_overrides'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url',
         ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      self.type_notebook_ssl_from_slave_kedifa_overrides_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = \
        createSelfSignedCertificate([parameter_dict['domain']])

    # as now the place to put the key is known put the key there
    auth = requests.get(
      generate_auth,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, auth.status_code)

    data = certificate_pem + key_pem

    upload = requests.put(
      upload_url + auth.text,
      data=data,
      verify=self.ca_certificate_file)
    self.assertEqual(httplib.CREATED, upload.status_code)

    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path',
      HTTPS_PORT)

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  @skip('Not implemented in new test system')
  def test_custom_domain_ssl_crt_ssl_key(self):
    reference = 'custom_domain_ssl_crt_ssl_key'
    parameter_dict = self.parseSlaveParameterDict(reference)
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)

    hostname = reference.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': ['ssl_key is obsolete, please use key-upload-url',
                         'ssl_crt is obsolete, please use key-upload-url']
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.customdomain_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_ssl_ca_crt(self):
    parameter_dict = self.parseSlaveParameterDict(
      'custom_domain_ssl_crt_ssl_key_ssl_ca_crt')
    self.assertLogAccessUrlWithPop(parameter_dict)
    generate_auth, upload_url = self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'customdomainsslcrtsslkeysslcacrt.example.com',
        'replication_number': '1',
        'url': 'http://customdomainsslcrtsslkeysslcacrt.example.com',
        'site_url': 'http://customdomainsslcrtsslkeysslcacrt.example.com',
        'secure_access':
        'https://customdomainsslcrtsslkeysslcacrt.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_ca_crt is obsolete, please use key-upload-url',
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url'
        ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.customdomain_ca_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'bbb-ssl',
      '_custom_domain_ssl_crt_ssl_key_ssl_ca_crt.crt'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      expected = self.customdomain_ca_certificate_pem + '\n' + \
        self.ca.certificate_pem + '\n' + self.customdomain_ca_key_pem
      self.assertEqual(
        expected,
        out.read()
      )

    ca = CertificateAuthority(
      'TestSlaveSlapOSMasterCertificateCompatibility')

    _, customdomain_ca_key_pem, csr, _ = createCSR(
      'customdomainsslcrtsslkeysslcacrt.example.com')
    _, customdomain_ca_certificate_pem = ca.signCSR(csr)

    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'custom_domain_ssl_crt_ssl_key_ssl_ca_crt'].copy()
    slave_parameter_dict.update(
      ssl_crt=customdomain_ca_certificate_pem,
      ssl_key=customdomain_ca_key_pem,
      ssl_ca_crt=ca.certificate_pem,
    )

    self.requestSlaveInstance(
        partition_reference='custom_domain_ssl_crt_ssl_key_ssl_ca_crt',
        partition_parameter_kw=slave_parameter_dict,
    )

    self.slap.waitForInstance()
    self.runKedifaUpdater()
    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      customdomain_ca_certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'bbb-ssl',
      '_custom_domain_ssl_crt_ssl_key_ssl_ca_crt.crt'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      expected = customdomain_ca_certificate_pem + '\n' + ca.certificate_pem \
        + '\n' + customdomain_ca_key_pem
      self.assertEqual(
        expected,
        out.read()
      )

  def test_ssl_ca_crt_garbage(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_ca_crt_garbage')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslcacrtgarbage.example.com',
        'replication_number': '1',
        'url': 'http://sslcacrtgarbage.example.com',
        'site_url': 'http://sslcacrtgarbage.example.com',
        'secure_access':
        'https://sslcacrtgarbage.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_ca_crt is obsolete, please use key-upload-url',
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url']
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
        parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.sslcacrtgarbage_ca_certificate_pem,
      der2pem(result.peercert)
    )

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_ssl_ca_crt_does_not_match(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_ca_crt_does_not_match')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'sslcacrtdoesnotmatch.example.com',
        'replication_number': '1',
        'url': 'http://sslcacrtdoesnotmatch.example.com',
        'site_url': 'http://sslcacrtdoesnotmatch.example.com',
        'secure_access':
        'https://sslcacrtdoesnotmatch.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
        'warning-list': [
          'ssl_ca_crt is obsolete, please use key-upload-url',
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url'
        ]
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    certificate_file_list = glob.glob(os.path.join(
      self.instance_path, '*', 'srv', 'bbb-ssl',
      '_ssl_ca_crt_does_not_match.crt'))
    self.assertEqual(1, len(certificate_file_list))
    certificate_file = certificate_file_list[0]
    with open(certificate_file) as out:
      expected = self.certificate_pem + '\n' + self.ca.certificate_pem + \
        '\n' + self.key_pem
      self.assertEqual(
        expected,
        out.read()
      )


class TestSlaveSlapOSMasterCertificateCompatibilityUpdate(
  SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def setUpMaster(cls):
    # run partition until AIKC finishes
    cls.runComputerPartitionUntil(
      cls.untilNotReadyYetNotInMasterKeyGenerateAuthUrl)

    parameter_dict = cls.requestDefaultInstance().getConnectionParameterDict()
    ca_certificate = requests.get(
      parameter_dict['kedifa-caucase-url'] + '/cas/crt/ca.crt.pem')
    assert ca_certificate.status_code == httplib.OK
    cls.ca_certificate_file = os.path.join(cls.working_directory, 'ca.crt.pem')
    open(cls.ca_certificate_file, 'w').write(ca_certificate.text)
    # Do not upload certificates for the master partition

  instance_parameter_dict = {
    'domain': 'example.com',
    'port': HTTPS_PORT,
    'plain_http_port': HTTP_PORT,
    'kedifa_port': KEDIFA_PORT,
    'caucase_port': CAUCASE_PORT,
    'mpm-graceful-shutdown-timeout': 2,
  }

  @classmethod
  def getInstanceParameterDict(cls):
    if 'apache-certificate' not in cls.instance_parameter_dict:
      cls.instance_parameter_dict.update(**{
        'apache-certificate': cls.certificate_pem,
        'apache-key': cls.key_pem,
      })
    return cls.instance_parameter_dict

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'ssl_from_master': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
    }

  def test_master_partition_state(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    expected_parameter_dict = {
      'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
      'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      'domain': 'example.com',
      'accepted-slave-amount': '1',
      'rejected-slave-amount': '0',
      'rejected-slave-dict': {},
      'slave-amount': '1',
      'warning-list': [
        u'apache-certificate is obsolete, please use master-key-upload-url',
        u'apache-key is obsolete, please use master-key-upload-url',
      ],
    }

    self.assertEqual(
      expected_parameter_dict,
      parameter_dict
    )

  def test_apache_key_apache_certificate_update(self):
    parameter_dict = self.parseSlaveParameterDict('ssl_from_master')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, '')
    hostname = 'ssl_from_master'.translate(None, '_-')
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

    _, key_pem, _, certificate_pem = createSelfSignedCertificate(
      [
        '*.customdomain.example.com',
        '*.example.com',
        '*.alias1.example.com',
      ])

    self.instance_parameter_dict.update(**{
      'apache-certificate': certificate_pem,
      'apache-key': key_pem,

    })
    self.requestDefaultInstance()
    self.slap.waitForInstance()
    self.runKedifaUpdater()

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')


class TestSlaveCiphers(SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
      'ciphers': 'ECDHE-ECDSA-AES256-GCM-SHA384 ECDHE-RSA-AES256-GCM-SHA384'
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    return {
      'default_ciphers': {
        'url': cls.backend_url,
        'enable_cache': True,
      },
      'own_ciphers': {
        'ciphers': 'ECDHE-ECDSA-AES128-GCM-SHA256 ECDHE-RSA-AES128-GCM-SHA256',
        'url': cls.backend_url,
        'enable_cache': True,
      },
    }

  def test_master_partition_state(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    expected_parameter_dict = {
      'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
      'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      'domain': 'example.com',
      'accepted-slave-amount': '2',
      'rejected-slave-amount': '0',
      'slave-amount': '2',
      'rejected-slave-dict': {}
    }

    self.assertEqual(
      expected_parameter_dict,
      parameter_dict
    )

  def test_default_ciphers(self):
    parameter_dict = self.assertSlaveBase('default_ciphers')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.OK, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(httplib.FOUND, result_http.status_code)

    configuration_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'caddy-slave-conf.d',
        '_default_ciphers.conf'
      ))[0]
    self.assertTrue(
      'ciphers ECDHE-ECDSA-AES256-GCM-SHA384 ECDHE-RSA-AES256-GCM-SHA384'
      in open(configuration_file).read()
    )

  def test_own_ciphers(self):
    parameter_dict = self.assertSlaveBase('own_ciphers')

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.OK, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(httplib.FOUND, result_http.status_code)

    configuration_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'caddy-slave-conf.d',
        '_own_ciphers.conf'
      ))[0]
    self.assertTrue(
      'ciphers ECDHE-ECDSA-AES128-GCM-SHA256 ECDHE-RSA-AES128-GCM-SHA256'
      in open(configuration_file).read()
    )


class TestSlaveRejectReportUnsafeDamaged(SlaveHttpFrontendTestCase):
  @classmethod
  def prepareCertificate(cls):
    cls.ca = CertificateAuthority('TestSlaveRejectReportUnsafeDamaged')
    super(TestSlaveRejectReportUnsafeDamaged, cls).prepareCertificate()

  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
    }

  @classmethod
  def setUpClass(cls):
    super(TestSlaveRejectReportUnsafeDamaged, cls).setUpClass()
    cls.fillSlaveParameterDictDict()
    cls.requestSlaves()
    try:
      cls.slap.waitForInstance(
        max_retry=2  # two runs shall be enough
      )
    except Exception:
      # ignores exceptions, as problems are tested
      pass
    cls.updateSlaveConnectionParameterDictDict()

  slave_parameter_dict_dict = {}

  @classmethod
  def getSlaveParameterDictDict(cls):
    return cls.slave_parameter_dict_dict

  @classmethod
  def fillSlaveParameterDictDict(cls):
    cls.slave_parameter_dict_dict = {
      'URL': {
        'url': "https://[fd46::c2ae]:!py!u'123123'",
      },
      'HTTPS-URL': {
        'https-url': "https://[fd46::c2ae]:!py!u'123123'",
      },
      'SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED': {
        'url': cls.backend_https_url,
        'ssl-proxy-verify': True,
        'ssl_proxy_ca_crt': 'damaged',
      },
      'SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY': {
        'url': cls.backend_https_url,
        'ssl-proxy-verify': True,
        'ssl_proxy_ca_crt': '',
      },
      'health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED': {
        'url': cls.backend_https_url,
        'health-check-failover-ssl-proxy-verify': True,
        'health-check-failover-ssl-proxy-ca-crt': 'damaged',
      },
      'health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY': {
        'url': cls.backend_https_url,
        'health-check-failover-ssl-proxy-verify': True,
        'health-check-failover-ssl-proxy-ca-crt': '',
      },
      'BAD-BACKEND': {
        'url': 'http://1:2:3:4',
        'https-url': 'http://host.domain:badport',
      },
      'EMPTY-BACKEND': {
        'url': '',
        'https-url': '',
      },
      'CUSTOM_DOMAIN-UNSAFE': {
        'custom_domain': '${section:option} afterspace\nafternewline',
      },
      'SERVER-ALIAS-UNSAFE': {
        'server-alias': '${section:option} afterspace',
      },
      'SERVER-ALIAS-SAME': {
        'url': cls.backend_url,
        'server-alias': 'serveraliassame.example.com',
      },
      'VIRTUALHOSTROOT-HTTP-PORT-UNSAFE': {
        'type': 'zope',
        'url': cls.backend_url,
        'virtualhostroot-http-port': '${section:option}',
      },
      'VIRTUALHOSTROOT-HTTPS-PORT-UNSAFE': {
        'type': 'zope',
        'url': cls.backend_url,
        'virtualhostroot-https-port': '${section:option}',
      },
      'DEFAULT-PATH-UNSAFE': {
        'type': 'zope',
        'url': cls.backend_url,
        'default-path': '${section:option}\nn"\newline\n}\n}proxy\n/slashed',
      },
      'MONITOR-IPV4-TEST-UNSAFE': {
        'monitor-ipv4-test': '${section:option}\nafternewline ipv4',
      },
      'MONITOR-IPV6-TEST-UNSAFE': {
        'monitor-ipv6-test': '${section:option}\nafternewline ipv6',
      },
      'BAD-CIPHERS': {
        'ciphers': 'bad ECDHE-ECDSA-AES256-GCM-SHA384 again',
      },
      'SITE_1': {
        'custom_domain': 'duplicate.example.com',
      },
      'SITE_2': {
        'custom_domain': 'duplicate.example.com',
      },
      'SITE_3': {
        'server-alias': 'duplicate.example.com',
      },
      'SITE_4': {
        'custom_domain': 'duplicate.example.com',
        'server-alias': 'duplicate.example.com',
      },
      'SSL_CA_CRT_ONLY': {
        'url': cls.backend_url,
        'ssl_ca_crt': cls.ca.certificate_pem,
      },
      'SSL_KEY-SSL_CRT-UNSAFE': {
        'ssl_key': '${section:option}ssl_keyunsafe\nunsafe',
        'ssl_crt': '${section:option}ssl_crtunsafe\nunsafe',
      },
      'health-check-http-method': {
        'health-check': True,
        'health-check-http-method': 'WRONG',
      },
      'health-check-http-version': {
        'health-check': True,
        'health-check-http-version': 'WRONG/1.1',
      },
      'health-check-timeout': {
        'health-check': True,
        'health-check-timeout': 'WRONG',
      },
      'health-check-timeout-negative': {
        'health-check': True,
        'health-check-timeout': '-2',
      },
      'health-check-interval': {
        'health-check': True,
        'health-check-interval': 'WRONG',
      },
      'health-check-interval-negative': {
        'health-check': True,
        'health-check-interval': '-2',
      },
      'health-check-rise': {
        'health-check': True,
        'health-check-rise': 'WRONG',
      },
      'health-check-rise-negative': {
        'health-check': True,
        'health-check-rise': '-2',
      },
      'health-check-fall': {
        'health-check': True,
        'health-check-fall': 'WRONG',
      },
      'health-check-fall-negative': {
        'health-check': True,
        'health-check-fall': '-2',
      }
    }

  def test_master_partition_state(self):
    parameter_dict = self.parseConnectionParameterDict()
    self.assertKeyWithPop('monitor-setup-url', parameter_dict)
    self.assertBackendHaproxyStatisticUrl(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertRejectedSlavePromiseWithPop(parameter_dict)

    expected_parameter_dict = {
      'monitor-base-url': 'https://[%s]:8401' % self._ipv6_address,
      'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      'domain': 'example.com',
      'accepted-slave-amount': '5',
      'rejected-slave-amount': '28',
      'slave-amount': '33',
      'rejected-slave-dict': {
        '_HTTPS-URL': ['slave https-url "https://[fd46::c2ae]:!py!u\'123123\'"'
                       ' invalid'],
        '_URL': [u'slave url "https://[fd46::c2ae]:!py!u\'123123\'" invalid'],
        '_SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED': [
          'ssl_proxy_ca_crt is invalid'
        ],
        '_SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY': [
          'ssl_proxy_ca_crt is invalid'
        ],
        '_BAD-CIPHERS': [
          "Cipher 'again' is not supported.",
          "Cipher 'bad' is not supported."
        ],
        '_CUSTOM_DOMAIN-UNSAFE': [
          "custom_domain '${section:option} afterspace\\nafternewline' invalid"
        ],
        '_SERVER-ALIAS-UNSAFE': [
          "server-alias '${section:option}' not valid",
          "server-alias 'afterspace' not valid"
        ],
        '_SITE_2': ["custom_domain 'duplicate.example.com' clashes"],
        '_SITE_3': ["server-alias 'duplicate.example.com' clashes"],
        '_SITE_4': ["custom_domain 'duplicate.example.com' clashes"],
        '_SSL_CA_CRT_ONLY': [
          "ssl_ca_crt is present, so ssl_crt and ssl_key are required"],
        '_SSL_KEY-SSL_CRT-UNSAFE': [
          "slave ssl_key and ssl_crt does not match"],
        '_BAD-BACKEND': [
          "slave https-url 'http://host.domain:badport' invalid",
          "slave url 'http://1:2:3:4' invalid"],
        '_VIRTUALHOSTROOT-HTTP-PORT-UNSAFE': [
          "Wrong virtualhostroot-http-port '${section:option}'"],
        '_VIRTUALHOSTROOT-HTTPS-PORT-UNSAFE': [
          "Wrong virtualhostroot-https-port '${section:option}'"],
        '_EMPTY-BACKEND': [
          "slave https-url '' invalid",
          "slave url '' invalid"],
        '_health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED': [
          'health-check-failover-ssl-proxy-ca-crt is invalid'
        ],
        '_health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY': [
          'health-check-failover-ssl-proxy-ca-crt is invalid'
        ],
        '_health-check-fall': [
          'Wrong health-check-fall WRONG'],
        '_health-check-fall-negative': [
          'Wrong health-check-fall -2'],
        '_health-check-http-method': [
          'Wrong health-check-http-method WRONG'],
        '_health-check-http-version': [
          'Wrong health-check-http-version WRONG/1.1'],
        '_health-check-interval': [
          'Wrong health-check-interval WRONG'],
        '_health-check-interval-negative': [
          'Wrong health-check-interval -2'],
        '_health-check-rise': [
          'Wrong health-check-rise WRONG'],
        '_health-check-rise-negative': [
          'Wrong health-check-rise -2'],
        '_health-check-timeout': [
          'Wrong health-check-timeout WRONG'],
        '_health-check-timeout-negative': [
          'Wrong health-check-timeout -2'],
      },
      'warning-slave-dict': {
        '_SSL_CA_CRT_ONLY': [
          'ssl_ca_crt is obsolete, please use key-upload-url'],
        '_SSL_KEY-SSL_CRT-UNSAFE': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url']}
    }

    self.assertEqual(
      expected_parameter_dict,
      parameter_dict
    )

  def test_url(self):
    parameter_dict = self.parseSlaveParameterDict('URL')
    self.assertEqual(
      {
        'request-error-list': [
          "slave url \"https://[fd46::c2ae]:!py!u'123123'\" invalid"]
      },
      parameter_dict
    )

  def test_https_url(self):
    parameter_dict = self.parseSlaveParameterDict('HTTPS-URL')
    self.assertEqual(
      {
        'request-error-list': [
          "slave https-url \"https://[fd46::c2ae]:!py!u'123123'\" invalid"]
      },
      parameter_dict
    )

  def test_ssl_proxy_verify_ssl_proxy_ca_crt_damaged(self):
    parameter_dict = self.parseSlaveParameterDict(
      'SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED')
    self.assertEqual(
      {'request-error-list': ["ssl_proxy_ca_crt is invalid"]},
      parameter_dict
    )

  def test_ssl_proxy_verify_ssl_proxy_ca_crt_empty(self):
    parameter_dict = self.parseSlaveParameterDict(
      'SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY')
    self.assertEqual(
      {'request-error-list': ["ssl_proxy_ca_crt is invalid"]},
      parameter_dict
    )

  def test_health_check_failover_ssl_proxy_ca_crt_damaged(self):
    parameter_dict = self.parseSlaveParameterDict(
      'health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_DAMAGED')
    self.assertEqual(
      {
        'request-error-list': [
          "health-check-failover-ssl-proxy-ca-crt is invalid"]
      },
      parameter_dict
    )

  def test_health_check_failover_ssl_proxy_ca_crt_empty(self):
    parameter_dict = self.parseSlaveParameterDict(
      'health-check-failover-SSL-PROXY-VERIFY_SSL_PROXY_CA_CRT_EMPTY')
    self.assertEqual(
      {
        'request-error-list': [
          "health-check-failover-ssl-proxy-ca-crt is invalid"]
      },
      parameter_dict
    )

  def test_server_alias_same(self):
    parameter_dict = self.parseSlaveParameterDict('SERVER-ALIAS-SAME')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'serveraliassame.example.com',
        'replication_number': '1',
        'url': 'http://serveraliassame.example.com',
        'site_url': 'http://serveraliassame.example.com',
        'secure_access': 'https://serveraliassame.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_custom_domain_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('CUSTOM_DOMAIN-UNSAFE')
    self.assertEqual(
      {
        'request-error-list': [
          "custom_domain '${section:option} afterspace\\nafternewline' invalid"
        ]
      },
      parameter_dict
    )

  def test_server_alias_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('SERVER-ALIAS-UNSAFE')
    self.assertEqual(
      {
        'request-error-list': [
          "server-alias '${section:option}' not valid", "server-alias "
          "'afterspace' not valid"]
      },
      parameter_dict
    )

  def test_bad_ciphers(self):
    parameter_dict = self.parseSlaveParameterDict('BAD-CIPHERS')
    self.assertEqual(
      {
        'request-error-list': [
          "Cipher 'again' is not supported.",
          "Cipher 'bad' is not supported."
        ]
      },
      parameter_dict
    )

  def test_virtualhostroot_http_port_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict(
      'VIRTUALHOSTROOT-HTTP-PORT-UNSAFE')
    self.assertEqual(
      {
        'request-error-list': [
          "Wrong virtualhostroot-http-port '${section:option}'"
        ]
      },
      parameter_dict
    )

  def test_virtualhostroot_https_port_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict(
      'VIRTUALHOSTROOT-HTTPS-PORT-UNSAFE')
    self.assertEqual(
      {
        'request-error-list': [
          "Wrong virtualhostroot-https-port '${section:option}'"
        ]
      },
      parameter_dict
    )

  def default_path_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('DEFAULT-PATH-UNSAFE')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict, 'master-')
    self.assertEqual(
      {
        'domain': 'defaultpathunsafe.example.com',
        'replication_number': '1',
        'url': 'http://defaultpathunsafe.example.com',
        'site_url': 'http://defaultpathunsafe.example.com',
        'secure_access': 'https://defaultpathunsafe.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], '')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(
      httplib.MOVED_PERMANENTLY,
      result.status_code
    )

    self.assertEqual(
      'https://defaultpathunsafe.example.com:%s/%%24%%7Bsection%%3Aoption%%7D'
      '%%0An%%22%%0Aewline%%0A%%7D%%0A%%7Dproxy%%0A/slashed' % (HTTPS_PORT,),
      result.headers['Location']
    )

  def test_monitor_ipv4_test_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('MONITOR-IPV4-TEST-UNSAFE')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'monitoripv4testunsafe.example.com',
        'replication_number': '1',
        'url': 'http://monitoripv4testunsafe.example.com',
        'site_url': 'http://monitoripv4testunsafe.example.com',
        'secure_access': 'https://monitoripv4testunsafe.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(httplib.FOUND, result_http.status_code)

    monitor_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        'check-_MONITOR-IPV4-TEST-UNSAFE-ipv4-packet-list-test.py'))[0]
    # get promise module and check that parameters are ok

    self.assertEqual(
      getPromisePluginParameterDict(monitor_file),
      {
        'frequency': '720',
        'ipv4': 'true',
        'address': '${section:option}\nafternewline ipv4',
      }
    )

  def test_monitor_ipv6_test_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('MONITOR-IPV6-TEST-UNSAFE')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'monitoripv6testunsafe.example.com',
        'replication_number': '1',
        'url': 'http://monitoripv6testunsafe.example.com',
        'site_url': 'http://monitoripv6testunsafe.example.com',
        'secure_access': 'https://monitoripv6testunsafe.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

    result = fakeHTTPSResult(
      parameter_dict['domain'], 'test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqual(httplib.SERVICE_UNAVAILABLE, result.status_code)

    result_http = fakeHTTPResult(
      parameter_dict['domain'], 'test-path')
    self.assertEqual(httplib.FOUND, result_http.status_code)

    monitor_file = glob.glob(
      os.path.join(
        self.instance_path, '*', 'etc', 'plugin',
        'check-_MONITOR-IPV6-TEST-UNSAFE-ipv6-packet-list-test.py'))[0]
    # get promise module and check that parameters are ok
    self.assertEqual(
      getPromisePluginParameterDict(monitor_file),
      {
        'frequency': '720',
        'address': '${section:option}\nafternewline ipv6'
      }
    )

  def test_site_1(self):
    parameter_dict = self.parseSlaveParameterDict('SITE_1')
    self.assertLogAccessUrlWithPop(parameter_dict)
    self.assertKedifaKeysWithPop(parameter_dict)
    self.assertEqual(
      {
        'domain': 'duplicate.example.com',
        'replication_number': '1',
        'url': 'http://duplicate.example.com',
        'site_url': 'http://duplicate.example.com',
        'secure_access': 'https://duplicate.example.com',
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict
    )

  def test_site_2(self):
    parameter_dict = self.parseSlaveParameterDict('SITE_2')
    self.assertEqual(
      {
        'request-error-list': ["custom_domain 'duplicate.example.com' clashes"]
      },
      parameter_dict
    )

  def test_site_3(self):
    parameter_dict = self.parseSlaveParameterDict('SITE_3')
    self.assertEqual(
      {
        'request-error-list': ["server-alias 'duplicate.example.com' clashes"]
      },
      parameter_dict,
    )

  def test_site_4(self):
    parameter_dict = self.parseSlaveParameterDict('SITE_4')
    self.assertEqual(
      {
        'request-error-list': ["custom_domain 'duplicate.example.com' clashes"]
      },
      parameter_dict
    )

  def test_ssl_ca_crt_only(self):
    parameter_dict = self.parseSlaveParameterDict('SSL_CA_CRT_ONLY')

    self.assertEqual(
      parameter_dict,
      {
        'request-error-list': [
          "ssl_ca_crt is present, so ssl_crt and ssl_key are required"],
        'warning-list': [
          'ssl_ca_crt is obsolete, please use key-upload-url',
        ],
      }
    )

  def test_ssl_key_ssl_crt_unsafe(self):
    parameter_dict = self.parseSlaveParameterDict('SSL_KEY-SSL_CRT-UNSAFE')
    self.assertEqual(
      {
        'request-error-list': ["slave ssl_key and ssl_crt does not match"],
        'warning-list': [
          'ssl_crt is obsolete, please use key-upload-url',
          'ssl_key is obsolete, please use key-upload-url']
      },
      parameter_dict
    )

  def test_bad_backend(self):
    parameter_dict = self.parseSlaveParameterDict('BAD-BACKEND')
    self.assertEqual(
      {
        'request-error-list': [
          "slave https-url 'http://host.domain:badport' invalid",
          "slave url 'http://1:2:3:4' invalid"],
      },
      parameter_dict
    )

  def test_empty_backend(self):
    parameter_dict = self.parseSlaveParameterDict('EMPTY-BACKEND')
    self.assertEqual(
      {
        'request-error-list': [
          "slave https-url '' invalid",
          "slave url '' invalid"]
      },
      parameter_dict
    )


class TestSlaveHostHaproxyClash(SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
      'request-timeout': '12',
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    # Note: The slaves are specifically constructed to have an order which
    #       is triggering the problem. Slave list is sorted in many places,
    #       and such slave configuration will result with them begin seen
    #       by backend haproxy configuration in exactly the way seen below
    #       Ordering it here will not help at all.
    return {
      'wildcard': {
        'url': cls.backend_url + 'wildcard',
        'custom_domain': '*.alias1.example.com',
      },
      'zspecific': {
        'url': cls.backend_url + 'zspecific',
        'custom_domain': 'zspecific.alias1.example.com',
      },
    }

  def test(self):
    parameter_dict_wildcard = self.parseSlaveParameterDict('wildcard')
    self.assertLogAccessUrlWithPop(parameter_dict_wildcard)
    self.assertKedifaKeysWithPop(parameter_dict_wildcard, '')
    hostname = '*.alias1'
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict_wildcard
    )
    parameter_dict_specific = self.parseSlaveParameterDict('zspecific')
    self.assertLogAccessUrlWithPop(parameter_dict_specific)
    self.assertKedifaKeysWithPop(parameter_dict_specific, '')
    hostname = 'zspecific.alias1'
    self.assertEqual(
      {
        'domain': '%s.example.com' % (hostname,),
        'replication_number': '1',
        'url': 'http://%s.example.com' % (hostname, ),
        'site_url': 'http://%s.example.com' % (hostname, ),
        'secure_access': 'https://%s.example.com' % (hostname, ),
        'backend-client-caucase-url': 'http://[%s]:8990' % self._ipv6_address,
      },
      parameter_dict_specific
    )

    result_wildcard = fakeHTTPSResult(
      'other.alias1.example.com',
      'test-path',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
      }
    )
    self.assertEqual(self.certificate_pem, der2pem(result_wildcard.peercert))
    self.assertEqualResultJson(result_wildcard, 'Path', '/wildcard/test-path')

    result_specific = fakeHTTPSResult(
      'zspecific.alias1.example.com',
      'test-path',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
      }
    )
    self.assertEqual(self.certificate_pem, der2pem(result_specific.peercert))
    self.assertEqualResultJson(result_specific, 'Path', '/zspecific/test-path')


class TestPassedRequestParameter(HttpFrontendTestCase):
  # special SRs to check out
  frontend_2_sr = 'special_sr_for_2'
  frontend_3_sr = 'special_sr_for_3'
  kedifa_sr = 'special_sr_for_kedifa'

  @classmethod
  def setUpClass(cls):
    super(TestPassedRequestParameter, cls).setUpClass()
    cls.slap.supply(cls.frontend_2_sr, cls.slap._computer_id)
    cls.slap.supply(cls.frontend_3_sr, cls.slap._computer_id)
    cls.slap.supply(cls.kedifa_sr, cls.slap._computer_id)

  @classmethod
  def tearDownClass(cls):
    cls.slap.supply(
      cls.frontend_2_sr, cls.slap._computer_id, state="destroyed")
    cls.slap.supply(
      cls.frontend_3_sr, cls.slap._computer_id, state="destroyed")
    cls.slap.supply(
      cls.kedifa_sr, cls.slap._computer_id, state="destroyed")
    super(TestPassedRequestParameter, cls).tearDownClass()

  instance_parameter_dict = {
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
  }

  @classmethod
  def getInstanceParameterDict(cls):
    return cls.instance_parameter_dict

  def test(self):
    self.instance_parameter_dict.update({
      # master partition parameters
      '-frontend-quantity': 3,
      '-sla-2-computer_guid': self.slap._computer_id,
      '-sla-3-computer_guid': self.slap._computer_id,
      '-frontend-2-state': 'stopped',
      '-frontend-2-software-release-url': self.frontend_2_sr,
      '-frontend-3-state': 'stopped',
      '-frontend-3-software-release-url': self.frontend_3_sr,
      '-kedifa-software-release-url': self.kedifa_sr,
      'automatic-internal-kedifa-caucase-csr': False,
      'automatic-internal-backend-client-caucase-csr': False,
      # all nodes partition parameters
      'apache-certificate': self.certificate_pem,
      'apache-key': self.key_pem,
      'domain': 'example.com',
      'enable-http2-by-default': True,
      'global-disable-http2': True,
      'mpm-graceful-shutdown-timeout': 2,
      're6st-verification-url': 're6st-verification-url',
      'backend-connect-timeout': 2,
      'backend-connect-retries': 1,
      'ciphers': 'ciphers',
      'request-timeout': 100,
      'authenticate-to-backend': True,
      # specific parameters
      '-frontend-config-1-ram-cache-size': '512K',
      '-frontend-config-2-ram-cache-size': '256K',
    })

    # re-request instance with updated parameters
    self.requestDefaultInstance()

    # run once instance, it's only needed for later checks
    try:
      self.slap.waitForInstance()
    except Exception:
      pass

    computer = self.slap._slap.registerComputer('local')
    # state of parameters of all instances
    partition_parameter_dict_dict = {}
    for partition in computer.getComputerPartitionList():
      if partition.getState() == 'destroyed':
        continue
      parameter_dict = partition.getInstanceParameterDict()
      instance_title = parameter_dict['instance_title']
      if '_' in parameter_dict:
        # "flatten" the instance parameter
        parameter_dict = json.loads(parameter_dict['_'])
      partition_parameter_dict_dict[instance_title] = parameter_dict
      parameter_dict[
        'X-software_release_url'] = partition.getSoftwareRelease().getURI()

    base_software_url = self.getSoftwareURL()

    # drop some very varying parameters
    def assertKeyWithPop(d, k):
      self.assertIn(k, d)
      d.pop(k)
    assertKeyWithPop(
      partition_parameter_dict_dict['caddy-frontend-1'],
      'master-key-download-url')
    assertKeyWithPop(
      partition_parameter_dict_dict['caddy-frontend-2'],
      'master-key-download-url')
    assertKeyWithPop(
      partition_parameter_dict_dict['caddy-frontend-3'],
      'master-key-download-url')
    assertKeyWithPop(
      partition_parameter_dict_dict['testing partition 0'],
      'timestamp')
    assertKeyWithPop(
      partition_parameter_dict_dict['testing partition 0'],
      'ip_list')

    monitor_password = partition_parameter_dict_dict[
      'caddy-frontend-1'].pop('monitor-password')
    self.assertEqual(
      monitor_password,
      partition_parameter_dict_dict[
        'caddy-frontend-2'].pop('monitor-password')
    )
    self.assertEqual(
      monitor_password,
      partition_parameter_dict_dict[
        'caddy-frontend-3'].pop('monitor-password')
    )
    self.assertEqual(
      monitor_password,
      partition_parameter_dict_dict[
        'kedifa'].pop('monitor-password')
    )

    backend_client_caucase_url = u'http://[%s]:8990' % (self._ipv6_address,)
    kedifa_caucase_url = u'http://[%s]:15090' % (self._ipv6_address,)
    expected_partition_parameter_dict_dict = {
      'caddy-frontend-1': {
        'X-software_release_url': base_software_url,
        u'apache-certificate': unicode(self.certificate_pem),
        u'apache-key': unicode(self.key_pem),
        u'authenticate-to-backend': u'True',
        u'backend-client-caucase-url': backend_client_caucase_url,
        u'backend-connect-retries': u'1',
        u'backend-connect-timeout': u'2',
        u'ciphers': u'ciphers',
        u'cluster-identification': u'testing partition 0',
        u'domain': u'example.com',
        u'enable-http2-by-default': u'True',
        u'extra_slave_instance_list': u'[]',
        u'frontend-name': u'caddy-frontend-1',
        u'global-disable-http2': u'True',
        u'kedifa-caucase-url': kedifa_caucase_url,
        u'monitor-cors-domains': u'monitor.app.officejs.com',
        u'monitor-httpd-port': 8411,
        u'monitor-username': u'admin',
        u'mpm-graceful-shutdown-timeout': u'2',
        u'plain_http_port': '11080',
        u'port': '11443',
        u'ram-cache-size': u'512K',
        u're6st-verification-url': u're6st-verification-url',
        u'request-timeout': u'100',
        u'slave-kedifa-information': u'{}'
      },
      'caddy-frontend-2': {
        'X-software_release_url': self.frontend_2_sr,
        u'apache-certificate': unicode(self.certificate_pem),
        u'apache-key': unicode(self.key_pem),
        u'authenticate-to-backend': u'True',
        u'backend-client-caucase-url': backend_client_caucase_url,
        u'backend-connect-retries': u'1',
        u'backend-connect-timeout': u'2',
        u'ciphers': u'ciphers',
        u'cluster-identification': u'testing partition 0',
        u'domain': u'example.com',
        u'enable-http2-by-default': u'True',
        u'extra_slave_instance_list': u'[]',
        u'frontend-name': u'caddy-frontend-2',
        u'global-disable-http2': u'True',
        u'kedifa-caucase-url': kedifa_caucase_url,
        u'monitor-cors-domains': u'monitor.app.officejs.com',
        u'monitor-httpd-port': 8412,
        u'monitor-username': u'admin',
        u'mpm-graceful-shutdown-timeout': u'2',
        u'plain_http_port': u'11080',
        u'port': u'11443',
        u'ram-cache-size': u'256K',
        u're6st-verification-url': u're6st-verification-url',
        u'request-timeout': u'100',
        u'slave-kedifa-information': u'{}'
      },
      'caddy-frontend-3': {
        'X-software_release_url': self.frontend_3_sr,
        u'apache-certificate': unicode(self.certificate_pem),
        u'apache-key': unicode(self.key_pem),
        u'authenticate-to-backend': u'True',
        u'backend-client-caucase-url': backend_client_caucase_url,
        u'backend-connect-retries': u'1',
        u'backend-connect-timeout': u'2',
        u'ciphers': u'ciphers',
        u'cluster-identification': u'testing partition 0',
        u'domain': u'example.com',
        u'enable-http2-by-default': u'True',
        u'extra_slave_instance_list': u'[]',
        u'frontend-name': u'caddy-frontend-3',
        u'global-disable-http2': u'True',
        u'kedifa-caucase-url': kedifa_caucase_url,
        u'monitor-cors-domains': u'monitor.app.officejs.com',
        u'monitor-httpd-port': 8413,
        u'monitor-username': u'admin',
        u'mpm-graceful-shutdown-timeout': u'2',
        u'plain_http_port': u'11080',
        u'port': u'11443',
        u're6st-verification-url': u're6st-verification-url',
        u'request-timeout': u'100',
        u'slave-kedifa-information': u'{}'
      },
      'kedifa': {
        'X-software_release_url': self.kedifa_sr,
        u'caucase_port': u'15090',
        u'cluster-identification': u'testing partition 0',
        u'kedifa_port': u'15080',
        u'monitor-cors-domains': u'monitor.app.officejs.com',
        u'monitor-httpd-port': u'8402',
        u'monitor-username': u'admin',
        u'slave-list': []
      },
      'testing partition 0': {
        '-frontend-2-software-release-url': self.frontend_2_sr,
        '-frontend-2-state': 'stopped',
        '-frontend-3-software-release-url': self.frontend_3_sr,
        '-frontend-3-state': 'stopped',
        '-frontend-config-1-ram-cache-size': '512K',
        '-frontend-config-2-ram-cache-size': '256K',
        '-frontend-quantity': '3',
        '-kedifa-software-release-url': self.kedifa_sr,
        '-sla-2-computer_guid': 'local',
        '-sla-3-computer_guid': 'local',
        'X-software_release_url': base_software_url,
        'apache-certificate': unicode(self.certificate_pem),
        'apache-key': unicode(self.key_pem),
        'authenticate-to-backend': 'True',
        'automatic-internal-backend-client-caucase-csr': 'False',
        'automatic-internal-kedifa-caucase-csr': 'False',
        'backend-connect-retries': '1',
        'backend-connect-timeout': '2',
        'caucase_port': '15090',
        'ciphers': 'ciphers',
        'domain': 'example.com',
        'enable-http2-by-default': 'True',
        'full_address_list': [],
        'global-disable-http2': 'True',
        'instance_title': 'testing partition 0',
        'kedifa_port': '15080',
        'mpm-graceful-shutdown-timeout': '2',
        'plain_http_port': '11080',
        'port': '11443',
        're6st-verification-url': 're6st-verification-url',
        'request-timeout': '100',
        'root_instance_title': 'testing partition 0',
        'slap_software_type': 'RootSoftwareInstance',
        'slave_instance_list': []
      }
    }
    self.assertEqual(
      expected_partition_parameter_dict_dict,
      partition_parameter_dict_dict
    )


class TestSlaveHealthCheck(SlaveHttpFrontendTestCase, TestDataMixin):
  @classmethod
  def getInstanceParameterDict(cls):
    return {
      'domain': 'example.com',
      'port': HTTPS_PORT,
      'plain_http_port': HTTP_PORT,
      'kedifa_port': KEDIFA_PORT,
      'caucase_port': CAUCASE_PORT,
      'mpm-graceful-shutdown-timeout': 2,
      'request-timeout': '12',
    }

  @classmethod
  def getSlaveParameterDictDict(cls):
    cls.setUpAssertionDict()
    return {
      'health-check-disabled': {
        'url': cls.backend_url,
      },
      'health-check-default': {
        'url': cls.backend_url,
        'health-check': True,
      },
      'health-check-connect': {
        'url': cls.backend_url,
        'health-check': True,
        'health-check-http-method': 'CONNECT',
      },
      'health-check-custom': {
        'url': cls.backend_url,
        'health-check': True,
        'health-check-http-method': 'POST',
        'health-check-http-path': '/POST-path to be encoded',
        'health-check-http-version': 'HTTP/1.0',
        'health-check-timeout': '7',
        'health-check-interval': '15',
        'health-check-rise': '3',
        'health-check-fall': '7',
      },
      'health-check-failover-url': {
        'https-only': False,  # http and https access to check
        'health-check-timeout': 1,  # fail fast for test
        'health-check-interval': 1,  # fail fast for test
        'url': cls.backend_url + 'url',
        'https-url': cls.backend_url + 'https-url',
        'health-check': True,
        'health-check-http-path': '/health-check-failover-url',
        'health-check-failover-url': cls.backend_url + 'failover-url?a=b&c=',
        'health-check-failover-https-url':
        cls.backend_url + 'failover-https-url?a=b&c=',
      },
      'health-check-failover-url-auth-to-backend': {
        'https-only': False,  # http and https access to check
        'health-check-timeout': 1,  # fail fast for test
        'health-check-interval': 1,  # fail fast for test
        'url': cls.backend_url + 'url',
        'https-url': cls.backend_url + 'https-url',
        'health-check': True,
        'health-check-http-path': '/health-check-failover-url-auth-to-backend',
        'health-check-authenticate-to-failover-backend': True,
        'health-check-failover-url': 'https://%s:%s/failover-url?a=b&c=' % (
          cls._ipv4_address, cls._server_https_auth_port),
        'health-check-failover-https-url':
        'https://%s:%s/failover-https-url?a=b&c=' % (
          cls._ipv4_address, cls._server_https_auth_port),
      },
      'health-check-failover-url-ssl-proxy-verified': {
        'url': cls.backend_url,
        'health-check-timeout': 1,  # fail fast for test
        'health-check-interval': 1,  # fail fast for test
        'health-check': True,
        'health-check-http-path': '/health-check-failover-url-ssl-proxy'
        '-verified',
        'health-check-failover-url': cls.backend_https_url,
        'health-check-failover-ssl-proxy-verify': True,
        'health-check-failover-ssl-proxy-ca-crt':
        cls.test_server_ca.certificate_pem,
      },
      'health-check-failover-url-ssl-proxy-verify-unverified': {
        'url': cls.backend_url,
        'health-check-timeout': 1,  # fail fast for test
        'health-check-interval': 1,  # fail fast for test
        'health-check': True,
        'health-check-http-path': '/health-check-failover-url-ssl-proxy-verify'
        '-unverified',
        'health-check-failover-url': cls.backend_https_url,
        'health-check-failover-ssl-proxy-verify': True,
        'health-check-failover-ssl-proxy-ca-crt':
        cls.another_server_ca.certificate_pem,
      },
      'health-check-failover-url-ssl-proxy-verify-missing': {
        'url': cls.backend_url,
        'health-check-timeout': 1,  # fail fast for test
        'health-check-interval': 1,  # fail fast for test
        'health-check': True,
        'health-check-http-path': '/health-check-failover-url-ssl-proxy-verify'
        '-missing',
        'health-check-failover-url': cls.backend_https_url,
        'health-check-failover-ssl-proxy-verify': True,
      },
    }

  @classmethod
  def setUpAssertionDict(cls):
    backend = urlparse.urlparse(cls.backend_url).netloc
    cls.assertion_dict = {
      'health-check-disabled': """\
backend _health-check-disabled-http
  timeout server 12s
  timeout connect 5s
  retries 3
  server _health-check-disabled-backend-http %s""" % (backend,),
      'health-check-connect': """\
backend _health-check-connect-http
  timeout server 12s
  timeout connect 5s
  retries 3
  server _health-check-connect-backend-http %s   check inter 5s"""
      """ rise 1 fall 2
  timeout check 2s""" % (backend,),
      'health-check-custom': """\
backend _health-check-custom-http
  timeout server 12s
  timeout connect 5s
  retries 3
  server _health-check-custom-backend-http %s   check inter 15s"""
      """ rise 3 fall 7
  option httpchk POST /POST-path%%20to%%20be%%20encoded HTTP/1.0
  timeout check 7s""" % (backend,),
      'health-check-default': """\
backend _health-check-default-http
  timeout server 12s
  timeout connect 5s
  retries 3
  server _health-check-default-backend-http %s   check inter 5s"""
      """ rise 1 fall 2
  option httpchk GET / HTTP/1.1
  timeout check 2s""" % (backend, )
    }

  def _get_backend_haproxy_configuration(self):
    backend_configuration_file = glob.glob(os.path.join(
      self.instance_path, '*', 'etc', 'backend-haproxy.cfg'))[0]
    with open(backend_configuration_file) as fh:
      return fh.read()

  def _test(self, key):
    parameter_dict = self.assertSlaveBase(key)
    self.assertIn(
      self.assertion_dict[key],
      self._get_backend_haproxy_configuration()
    )
    result = fakeHTTPSResult(
      parameter_dict['domain'],
      'test-path/deep/.././deeper',
      headers={
        'Timeout': '10',  # more than default backend-connect-timeout == 5
        'Accept-Encoding': 'gzip',
      }
    )
    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path/deeper')

  def test_health_check_disabled(self):
    self._test('health-check-disabled')

  def test_health_check_default(self):
    self._test('health-check-default')

  def test_health_check_connect(self):
    self._test('health-check-connect')

  def test_health_check_custom(self):
    self._test('health-check-custom')

  def test_health_check_failover_url(self):
    parameter_dict = self.assertSlaveBase('health-check-failover-url')
    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'health-check-failover-url']

    # check normal access
    result = fakeHTTPResult(parameter_dict['domain'], '/path')
    self.assertEqualResultJson(result, 'Path', '/url/path')
    result = fakeHTTPSResult(parameter_dict['domain'], '/path')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(result, 'Path', '/https-url/path')

    # start replying with bad status code
    result = requests.put(
      self.backend_url + slave_parameter_dict[
        'health-check-http-path'].strip('/'),
      headers={'X-Reply-Status-Code': '502'})
    self.assertEqual(result.status_code, httplib.CREATED)

    time.sleep(3)  # > health-check-timeout + health-check-interval

    result = fakeHTTPSResult(parameter_dict['domain'], '/failoverpath')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(
      result, 'Path', '/failover-https-url?a=b&c=/failoverpath')

    self.assertLastLogLineRegexp(
      '_health-check-failover-url_backend_log',
      r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+ '
      r'\[\d{2}\/.{3}\/\d{4}\:\d{2}\:\d{2}\:\d{2}.\d{3}\] '
      r'https-backend _health-check-failover-url-https-failover'
      r'\/_health-check-failover-url-backend-https '
      r'\d+/\d+\/\d+\/\d+\/\d+ '
      r'200 \d+ - - ---- '
      r'\d+\/\d+\/\d+\/\d+\/\d+ \d+\/\d+ '
      r'"GET /failoverpath HTTP/1.1"'
    )

    result = fakeHTTPResult(parameter_dict['domain'], '/failoverpath')
    self.assertEqualResultJson(
      result, 'Path', '/failover-url?a=b&c=/failoverpath')
    self.assertLastLogLineRegexp(
      '_health-check-failover-url_backend_log',
      r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+ '
      r'\[\d{2}\/.{3}\/\d{4}\:\d{2}\:\d{2}\:\d{2}.\d{3}\] '
      r'http-backend _health-check-failover-url-http-failover'
      r'\/_health-check-failover-url-backend-http '
      r'\d+/\d+\/\d+\/\d+\/\d+ '
      r'200 \d+ - - ---- '
      r'\d+\/\d+\/\d+\/\d+\/\d+ \d+\/\d+ '
      r'"GET /failoverpath HTTP/1.1"'
    )

  def test_health_check_failover_url_auth_to_backend(self):
    parameter_dict = self.assertSlaveBase(
      'health-check-failover-url-auth-to-backend')
    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'health-check-failover-url-auth-to-backend']

    self.startAuthenticatedServerProcess()
    self.addCleanup(self.stopAuthenticatedServerProcess)
    # assert that you can't fetch nothing without key
    try:
      requests.get(self.backend_https_auth_url, verify=False)
    except Exception:
      pass
    else:
      self.fail(
        'Access to %r shall be not possible without certificate' % (
          self.backend_https_auth_url,))
    # check normal access
    result = fakeHTTPResult(parameter_dict['domain'], '/path')
    self.assertEqualResultJson(result, 'Path', '/url/path')
    self.assertNotIn('X-Backend-Identification', result.headers)
    result = fakeHTTPSResult(parameter_dict['domain'], '/path')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(result, 'Path', '/https-url/path')
    self.assertNotIn('X-Backend-Identification', result.headers)

    # start replying with bad status code
    result = requests.put(
      self.backend_url + slave_parameter_dict[
        'health-check-http-path'].strip('/'),
      headers={'X-Reply-Status-Code': '502'})
    self.assertEqual(result.status_code, httplib.CREATED)

    time.sleep(3)  # > health-check-timeout + health-check-interval

    result = fakeHTTPSResult(parameter_dict['domain'], '/failoverpath')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(
      result, 'Path', '/failover-https-url?a=b&c=/failoverpath')
    self.assertEqual(
      'Auth Backend', result.headers['X-Backend-Identification'])

    result = fakeHTTPResult(parameter_dict['domain'], '/failoverpath')
    self.assertEqualResultJson(
      result, 'Path', '/failover-url?a=b&c=/failoverpath')
    self.assertEqual(
      'Auth Backend', result.headers['X-Backend-Identification'])

  def test_health_check_failover_url_ssl_proxy_verified(self):
    parameter_dict = self.assertSlaveBase(
      'health-check-failover-url-ssl-proxy-verified')
    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'health-check-failover-url-ssl-proxy-verified']

    # check normal access
    result = fakeHTTPSResult(parameter_dict['domain'], '/path')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(result, 'Path', '/path')

    # start replying with bad status code
    result = requests.put(
      self.backend_url + slave_parameter_dict[
        'health-check-http-path'].strip('/'),
      headers={'X-Reply-Status-Code': '502'})
    self.assertEqual(result.status_code, httplib.CREATED)

    time.sleep(3)  # > health-check-timeout + health-check-interval

    result = fakeHTTPSResult(
      parameter_dict['domain'], '/test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    self.assertEqualResultJson(result, 'Path', '/test-path')

  def test_health_check_failover_url_ssl_proxy_unverified(self):
    parameter_dict = self.assertSlaveBase(
      'health-check-failover-url-ssl-proxy-verify-unverified')
    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'health-check-failover-url-ssl-proxy-verify-unverified']

    # check normal access
    result = fakeHTTPSResult(parameter_dict['domain'], '/path')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(result, 'Path', '/path')

    # start replying with bad status code
    result = requests.put(
      self.backend_url + slave_parameter_dict[
        'health-check-http-path'].strip('/'),
      headers={'X-Reply-Status-Code': '502'})
    self.assertEqual(result.status_code, httplib.CREATED)

    time.sleep(3)  # > health-check-timeout + health-check-interval

    result = fakeHTTPSResult(
      parameter_dict['domain'], '/test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    # as ssl proxy verification failed, service is unavailable
    self.assertEqual(result.status_code, httplib.SERVICE_UNAVAILABLE)

  def test_health_check_failover_url_ssl_proxy_missing(self):
    parameter_dict = self.assertSlaveBase(
      'health-check-failover-url-ssl-proxy-verify-missing')
    slave_parameter_dict = self.getSlaveParameterDictDict()[
      'health-check-failover-url-ssl-proxy-verify-missing']

    # check normal access
    result = fakeHTTPSResult(parameter_dict['domain'], '/path')
    self.assertEqual(self.certificate_pem, der2pem(result.peercert))
    self.assertEqualResultJson(result, 'Path', '/path')

    # start replying with bad status code
    result = requests.put(
      self.backend_url + slave_parameter_dict[
        'health-check-http-path'].strip('/'),
      headers={'X-Reply-Status-Code': '502'})
    self.assertEqual(result.status_code, httplib.CREATED)

    time.sleep(3)  # > health-check-timeout + health-check-interval

    result = fakeHTTPSResult(
      parameter_dict['domain'], '/test-path')

    self.assertEqual(
      self.certificate_pem,
      der2pem(result.peercert))

    # as ssl proxy verification failed, service is unavailable
    self.assertEqual(result.status_code, httplib.SERVICE_UNAVAILABLE)


if __name__ == '__main__':
  class HTTP6Server(ThreadedHTTPServer):
    address_family = socket.AF_INET6
  ip, port = sys.argv[1], int(sys.argv[2])
  if ':' in ip:
    klass = HTTP6Server
    url_template = 'http://[%s]:%s/'
  else:
    klass = ThreadedHTTPServer
    url_template = 'http://%s:%s/'

  server = klass((ip, port), TestHandler)
  print url_template % server.server_address[:2]
  server.serve_forever()
