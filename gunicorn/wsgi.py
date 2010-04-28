# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license. 
# See the NOTICE for more information.

import errno
import logging
import os
import re
import socket

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
    
import sys
from urllib import unquote

from simplehttp import RequestParser

from gunicorn import __version__
from gunicorn.util import CHUNK_SIZE, http_date, write, write_chunk, is_hoppish

NORMALIZE_SPACE = re.compile(r'(?:\r\n)?[ \t]+')

class RequestError(Exception):
    pass

class Response(object):
    
    def __init__(self, req, status, headers):
        self.req = req
        self.version = req.SERVER_VERSION
        self.status = status
        self.chunked = False
        self.headers = []
        self.headers_sent = False

        for name, value in headers:
            assert isinstance(name, basestring), "%r is not a string" % name
            if is_hoppish(name):
                lname = name.lower().strip()
                if lname == "transfer-encoding":
                    if value.lower().strip() == "chunked":
                        self.chunked = True
                elif lname == "connection":
                    # handle websocket
                    if value.lower().strip() != "upgrade":
                        continue
                else:
                    # ignore hopbyhop headers
                    continue
            self.headers.append((name.strip(), str(value).strip()))

    def default_headers(self):
        return [
            "HTTP/1.1 %s\r\n" % self.status,
            "Server: %s\r\n" % self.version,
            "Date: %s\r\n" % http_date(),
            "Connection: close\r\n"
        ]

    def send_headers(self):
        if self.headers_sent:
            return
        tosend = self.default_headers()
        tosend.extend(["%s: %s\r\n" % (n, v) for n, v in self.headers])
        write(self.req.socket, "%s\r\n" % "".join(tosend))
        self.headers_sent = True

    def write(self, arg):
        self.send_headers()
        assert isinstance(arg, basestring), "%r is not a string." % arg
        write(self.req.socket, arg, self.chunked)

    def close(self):
        if not self.headers_sent:
            self.send_headers()
        if self.chunked:
            write_chunk(self.req.socket, "")

class KeepAliveResponse(Response):

    def default_headers(self):
        connection = "keep-alive"
        if self.req.req.should_close():
            connection = "close"

        return [
            "HTTP/1.1 %s\r\n" % self.status,
            "Server: %s\r\n" % self.version,
            "Date: %s\r\n" % http_date(),
            "Connection: %s\r\n" % connection
        ]        
class Request(object):

    RESPONSE_CLASS = Response
    SERVER_VERSION = "gunicorn/%s" % __version__
    
    DEFAULTS = {
        "wsgi.url_scheme": 'http',
        "wsgi.input": StringIO(),
        "wsgi.errors": sys.stderr,
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
        "SCRIPT_NAME": "",
        "SERVER_SOFTWARE": "gunicorn/%s" % __version__
    }

    def __init__(self, socket, client_address, server_address, conf):
        self.debug = conf['debug']
        self.conf = conf
        self.socket = socket
    
        self.client_address = client_address
        self.server_address = server_address
        self.response_status = None
        self.response_headers = []
        self._version = 11
        self.parser = RequestParser(self.socket)
        self.log = logging.getLogger(__name__)
        self.response = None
        self.response_chunked = False
        self.headers_sent = False
        self.req = None

    def read(self):
        environ = {}
        headers = []
        
        ended = False
        req = None
        
        self.req = req = self.parser.next()
        
        ##self.log.debug("%s", self.parser.status)
        self.log.debug("Headers:\n%s" % req.headers)
        
        # authors should be aware that REMOTE_HOST and REMOTE_ADDR
        # may not qualify the remote addr:
        # http://www.ietf.org/rfc/rfc3875
        client_address = self.client_address or "127.0.0.1"
        forward_address = client_address
        server_address = self.server_address
        script_name = os.environ.get("SCRIPT_NAME", "")
        content_type = ""
        content_length = ""
        for hdr_name, hdr_value in req.headers:
            name = hdr_name.lower()
            if name == "expect":
                # handle expect
                if hdr_value.lower() == "100-continue":
                    self.socket.send("HTTP/1.1 100 Continue\r\n\r\n")
            elif name == "x-forwarded-for":
                forward_address = hdr_value
            elif name == "host":
                host = hdr_value
            elif name == "script_name":
                script_name = hdr_value
            elif name == "content-type":
                content_type = hdr_value
            elif name == "content-length":
                content_length = hdr_value
            else:
                continue
                
                        
        # This value should evaluate true if an equivalent application
        # object may be simultaneously invoked by another process, and
        # should evaluate false otherwise. In debug mode we fall to one
        # worker so we comply to pylons and other paster app.
        wsgi_multiprocess = (self.debug == False)

        if isinstance(forward_address, basestring):
            # we only took the last one
            # http://en.wikipedia.org/wiki/X-Forwarded-For
            if "," in forward_address:
                forward_adress = forward_address.split(",")[-1].strip()
            remote_addr = forward_address.split(":")
            if len(remote_addr) == 1:
                remote_addr.append('')
        else:
            remote_addr = forward_address

        if isinstance(server_address, basestring):
            server_address =  server_address.split(":")
            if len(server_address) == 1:
                server_address.append('')

        path_info = req.path
        if script_name:
            path_info = path_info.split(script_name, 1)[-1]


        environ = {
            "wsgi.url_scheme": 'http',
            "wsgi.input": req.body,
            "wsgi.errors": sys.stderr,
            "wsgi.version": (1, 0),
            "wsgi.multithread": False,
            "wsgi.multiprocess": wsgi_multiprocess,
            "wsgi.run_once": False,
            "SCRIPT_NAME": script_name,
            "SERVER_SOFTWARE": self.SERVER_VERSION,
            "REQUEST_METHOD": req.method,
            "PATH_INFO": unquote(path_info),
            "QUERY_STRING": req.query,
            "RAW_URI": req.path,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": content_length,
            "REMOTE_ADDR": remote_addr[0],
            "REMOTE_PORT": str(remote_addr[1]),
            "SERVER_NAME": server_address[0],
            "SERVER_PORT": str(server_address[1]),
            "SERVER_PROTOCOL": req.version
        }
        
        for key, value in req.headers:
            key = 'HTTP_' + key.upper().replace('-', '_')
            if key not in ('HTTP_CONTENT_TYPE', 'HTTP_CONTENT_LENGTH'):
                environ[key] = value
                
        return environ
        
    def start_response(self, status, headers, exc_info=None):
        if exc_info:
            try:
                if self.response and self.response.headers_sent:
                    raise exc_info[0], exc_info[1], exc_info[2]
            finally:
                exc_info = None
        elif self.response is not None:
            raise AssertionError("Response headers already set!")

        self.response = self.RESPONSE_CLASS(self, status, headers)
        return self.response.write

                   
class KeepAliveRequest(Request):

    RESPONSE_CLASS = KeepAliveResponse

    def read(self):
        try:
            return super(KeepAliveRequest, self).read()
        except socket.error, e:
            if e[0] == errno.ECONNRESET:
                return
            raise

