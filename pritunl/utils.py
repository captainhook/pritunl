from constants import *
from cache import cache_db
import flask
import json
import subprocess
import re
import urllib2
import httplib
import socket
import time
import base64
import hashlib
import os
import hmac
import uuid
import datetime
import logging

logger = logging.getLogger(APP_NAME)

def jsonify(data=None, status_code=None):
    if not isinstance(data, basestring):
        data = json.dumps(data)
    response = flask.Response(response=data, mimetype='application/json')
    response.headers.add('Cache-Control',
        'no-cache, no-store, must-revalidate')
    response.headers.add('Pragma', 'no-cache')
    response.headers.add('Expires', 0)
    if status_code is not None:
        response.status_code = status_code
    return response

def get_remote_addr():
    return flask.request.remote_addr

def rmtree(path):
    logged = False
    for _ in xrange(10):
        try:
            subprocess.check_call(['rm', '-rf', path])
            return
        except subprocess.CalledProcessError:
            time.sleep(0.01)
            if not logged:
                logged = True
                logger.exception('Remove tree error, retrying...')
    raise

def ip_to_long(ip_str):
    ip = ip_str.split('.')
    ip.reverse()
    while len(ip) < 4:
        ip.insert(1, '0')
    return sum(long(byte) << 8 * i for i, byte in enumerate(ip))

def long_to_ip(ip_num):
    return '.'.join(map(str, [
        (ip_num >> 24) & 0xff,
        (ip_num >> 16) & 0xff,
        (ip_num >> 8) & 0xff,
        ip_num & 0xff,
    ]))

def subnet_to_cidr(subnet):
    count = 0
    while ~ip_to_long(subnet) & pow(2, count):
        count += 1
    return 32 - count

def network_addr(ip, subnet):
    return '%s/%s' % (long_to_ip(ip_to_long(ip) & ip_to_long(subnet)),
        subnet_to_cidr(subnet))

def get_local_networks():
    addresses = []
    output = subprocess.check_output(['ifconfig'])
    for interface in output.split('\n\n'):
        interface_name = re.findall(r'[a-z0-9]+', interface, re.IGNORECASE)
        if not interface_name:
            continue
        interface_name = interface_name[0]
        if re.search(r'tun[0-9]+', interface_name) or interface_name == 'lo':
            continue
        addr = re.findall(r'inet.{0,10}' + IP_REGEX, interface, re.IGNORECASE)
        if not addr:
            continue
        addr = re.findall(IP_REGEX, addr[0], re.IGNORECASE)
        if not addr:
            continue
        mask = re.findall(r'mask.{0,10}' + IP_REGEX, interface, re.IGNORECASE)
        if not mask:
            continue
        mask = re.findall(IP_REGEX, mask[0], re.IGNORECASE)
        if not mask:
            continue
        addr = addr[0]
        mask = mask[0]
        if addr.split('.')[0] == '127':
            continue
        addresses.append(network_addr(addr, mask))
    return addresses

def get_cert_block(cert_data):
    start_index = cert_data.index('-----BEGIN CERTIFICATE-----')
    end_index = cert_data.index('-----END CERTIFICATE-----') + 25
    return cert_data[start_index:end_index]

def filter_str(in_str):
    if not in_str:
        return in_str
    return ''.join(x for x in in_str if x.isalnum() or x in NAME_SAFE_CHARS)

def check_openssl():
    try:
        # Check for unpatched heartbleed
        openssl_ver = subprocess.check_output(['openssl', 'version', '-a'])
        version, build_date = openssl_ver.split('\n')[0:2]

        build_date = build_date.replace('built on:', '').strip()
        build_date = build_date.split()
        build_date = ' '.join([build_date[1],
            build_date[2].zfill(2), build_date[5]])
        build_date = datetime.datetime.strptime(build_date, '%b %d %Y').date()

        if version in OPENSSL_HEARTBLEED and \
                build_date < OPENSSL_HEARTBLEED_BUILD_DATE:
            return False
    except:
        pass
    return True

class Response:
    def __init__(self, url, headers, status_code, reason, content):
        self.url = url
        self.headers = headers
        self.status_code = status_code
        self.reason = reason
        self.content = content

    def json(self):
        return json.loads(self.content)

class request:
    @classmethod
    def _request(cls, method, url, json_data=None, headers={},
            timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        data = None
        request = urllib2.Request(url, headers=headers)
        request.get_method = lambda: method

        if json_data is not None:
            request.add_header('Content-Type', 'application/json')
            data = json.dumps(json_data)

        try:
            url_response = urllib2.urlopen(request, data=data, timeout=timeout)
            return Response(url,
                headers=dict(url_response.info().items()),
                status_code=url_response.getcode(),
                reason='OK',
                content=url_response.read(),
            )
        except urllib2.HTTPError as error:
            return Response(url,
                headers=dict(error.info().items()),
                status_code=error.getcode(),
                reason=error.reason,
                content=error.read(),
            )
        except Exception as error:
            raise httplib.HTTPException(error)

    @classmethod
    def get(cls, url, **kwargs):
        return cls._request('GET', url, **kwargs)

    @classmethod
    def options(cls, url, **kwargs):
        return cls._request('OPTIONS', url, **kwargs)

    @classmethod
    def head(cls, url, **kwargs):
        return cls._request('HEAD', url, **kwargs)

    @classmethod
    def post(cls, url, **kwargs):
        return cls._request('POST', url, **kwargs)

    @classmethod
    def put(cls, url, **kwargs):
        return cls._request('PUT', url, **kwargs)

    @classmethod
    def patch(cls, url, **kwargs):
        return cls._request('PATCH', url, **kwargs)

    @classmethod
    def delete(cls, url, **kwargs):
        return cls._request('DELETE', url, **kwargs)


def check_session():
    from administrator import Administrator

    auth_token = flask.request.headers.get('Auth-Token', None)
    if auth_token:
        auth_timestamp = flask.request.headers.get('Auth-Timestamp', None)
        auth_nonce = flask.request.headers.get('Auth-Nonce', None)
        auth_signature = flask.request.headers.get('Auth-Signature', None)
        if not auth_token or not auth_timestamp or not auth_nonce or \
                not auth_signature:
            return False
        auth_nonce = auth_nonce[:32]

        try:
            if abs(int(auth_timestamp) - int(time.time())) > AUTH_TIME_WINDOW:
                return False
        except ValueError:
            return False

        # TODO
        cache_key = 'auth_nonce-%s' % auth_nonce
        if cache_db.exists(cache_key):
            return False

        administrator = Administrator.find_user(token=auth_token)
        if not administrator:
            return False

        auth_string = '&'.join([
            auth_token, auth_timestamp, auth_nonce, flask.request.method,
            flask.request.path] +
            ([flask.request.data] if flask.request.data else []))

        if len(auth_string) > AUTH_SIG_STRING_MAX_LEN:
            return False

        auth_test_signature = base64.b64encode(hmac.new(
            administrator.secret.encode(), auth_string,
            hashlib.sha256).digest())
        if auth_signature != auth_test_signature:
            return False

        # TODO
        cache_db.expire(cache_key, int(AUTH_TIME_WINDOW * 2.1))
        cache_db.set(cache_key, auth_timestamp)

        flask.request.administrator = administrator
    else:
        from pritunl import app_server
        if not flask.session:
            print 'error0'
            return False

        admin_id = flask.session.get('admin_id')
        if not admin_id:
            print 'error1'
            return False

        administrator = Administrator.get_user(id=admin_id)
        if not administrator:
            print 'error2'
            return False

        if not app_server.ssl and flask.session.get(
                'source') != get_remote_addr():
            flask.session.clear()
            print 'error3'
            return False

        if SESSION_TIMEOUT and int(time.time()) - \
                flask.session['timestamp'] > SESSION_TIMEOUT:
            flask.session.clear()
            print 'error4'
            return False

        flask.request.administrator = administrator
    return True

def check_auth(username, password, remote_addr=None):
    from administrator import Administrator

    if remote_addr:
        # TODO
        cache_key = 'ip_' + remote_addr
        count = cache_db.list_length(cache_key)
        if count and count > 10:
            raise flask.abort(403)

        # TODO
        key_exists = cache_db.exists(cache_key)
        cache_db.list_rpush(cache_key, '')
        if not key_exists:
            cache_db.expire(cache_key, 20)

    administrator = Administrator.find_user(username=username)
    if not administrator:
        return
    if not administrator.test_password(password):
        return
    return administrator
