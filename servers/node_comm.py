"""
A simple handler for running subprocess calls on
different nodes in SLURM systems
"""
import abc
import os
import socket, socketserver, json, traceback, subprocess
import sys

__all__ = [
    "NodeCommTCPServer",
    "NodeCommUnixServer",
    "NodeCommHandler",
    "NodeCommClient"
]

def infer_mode(connection):
    if (
            isinstance(connection, tuple)
            and isinstance(connection[0], str) and isinstance(connection[1], int)
    ):
        mode = "TCP"
    elif isinstance(connection, str):
        mode = "Unix"
    else:
        raise ValueError(f"invalid connection spec {connection}")
    return mode

class NodeCommTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

class NodeCommUnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def server_bind(self):
        """Called by constructor to bind the socket.

        May be overridden.

        """
        
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()

class NodeCommClient:
    def __init__(self, connection, timeout=10):
        self.conn = connection
        mode = infer_mode(connection)
        if mode == "TCP":
            self.mode = socket.AF_INET
        elif mode == "Unix":
            self.mode = socket.AF_UNIX
        else:
            raise NotImplementedError(mode)
        self.timeout = timeout

    def communicate(self, command, args):

        request = json.dumps({
            "command": command,
            "args": args
        }) + "\n"
        request = request.encode()

        # Create a socket (SOCK_STREAM means a TCP socket)
        mode = infer_mode(self.conn)
        # print(f"Sending request over {mode}")
        if mode == "Unix" and not os.path.exists(self.conn):
            raise ValueError(f"socket file {self.conn} doesn't exist")
        with socket.socket(self.mode, socket.SOCK_STREAM) as sock:
            # Connect to server and send data
            sock.connect(self.conn)
            sock.settimeout(self.timeout)
            sock.sendall(request)
            # Receive data from the server and shut down
            body = b''
            while b'\n' not in body:
                body = body + sock.recv(1024)

        response = json.loads(body.strip().decode())
        msg = response.get("stdout","")
        if len(msg) > 0: print(msg, file=sys.stdout)
        msg = response.get("stderr","")
        if len(msg) > 0: print(msg, file=sys.stderr)

class NodeCommHandler(socketserver.StreamRequestHandler):

    def handle(self):
        try:
            # self.rfile is a file-like object created by the handler;
            # we can now use e.g. readline() instead of raw recv() calls
            self.data = self.rfile.readline().strip()
            response = self.handle_json_request(self.data)
            # Likewise, self.wfile is a file-like object used to write back
            # to the client
        except:
            response = {
                "stdout": "",
                "stderr": traceback.format_exc(limit=1)
            }
        try:
            self.wfile.write(json.dumps(response).encode() + b'\n')
        except:
            traceback.print_exc(limit=1)  # big ol' fallback

    def handle_json_request(self, message: bytes):
        try:
            request = json.loads(message.decode())
        except:
            response = {
                "stdout": "",
                "stderr": traceback.format_exc(limit=1)
            }
        else:
            comm = request.get("command", '<unknown>')
            args = request.get("args", [])
            print(f"Got: {comm} {args}")
            response = self.dispatch_request(request)
            print(f"Sending: {response}")

        return response

    @property
    def method_dispatch(self): 
        return dict(
            {
                "cd": self.change_pwd,
                "pwd": self.get_pwd
            },
            **self.get_methods()
        )
    def change_pwd(self, args):
        os.chdir(args[0])
        return {
            'stdout':"",
            'stderr':""
        }
    def get_pwd(self, args):
        cwd = os.getcwd()
        return {
            'stdout':cwd,
            'stderr':""
        }
    def dispatch_request(self, request: dict):
        method = request.get("command", None)
        if method is None:
            response = {
                "stdout": "",
                "stderr": f"no command specified"
            }
        else:
            caller = self.method_dispatch.get(method.lower(), None)
            if caller is None:
                response = {
                    "stdout": "",
                    "stderr": f"unknown command {method}"
                }
            else:
                args = request.get("args", None)
                if args is None:
                    response = {
                        "stdout": "",
                        "stderr": f"malformatted request {request}"
                    }
                else:
                    try:
                        response = caller(args)
                    except:
                        response = {
                            "stdout": "",
                            "stderr": traceback.format_exc(limit=1)
                        }

        return response

    @classmethod
    def subprocess_response(cls, command, args):
        pipes = subprocess.Popen([command, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        std_out, std_err = pipes.communicate()
        return {
            "stdout":std_out.strip().decode(),
            "stderr":std_err.strip().decode()
        }
    @abc.abstractmethod
    def get_methods(self) -> 'dict[str,method]':
        ...

    TCP_SERVER = NodeCommTCPServer
    UNIX_SERVER = NodeCommUnixServer
    DEFAULT_CONNECTION = ("localhost", 9999)
    DEFAULT_PORT_ENV_VAR = None
    DEFAULT_SOCKET_ENV_VAR = None
    @classmethod
    def start_server(cls, connection=None, port=None):
        # Create the server, binding to localhost on port 9999
        if connection is None:
            connection = os.environ.get(cls.DEFAULT_SOCKET_ENV_VAR)
        if connection is None:
            if port is None:
                port = os.environ.get(cls.DEFAULT_PORT_ENV_VAR)
            if port is not None:
                connection = ('localhost', port)
        if connection is None:
            connection = cls.DEFAULT_CONNECTION
        mode = infer_mode(connection)
        print(f"Starting server at {connection} over {mode}")
        if mode == "TCP":
            server_type = cls.TCP_SERVER
        elif mode == "Unix":
            server_type = cls.UNIX_SERVER
        else:
            raise NotImplementedError(mode)
        with server_type(connection, cls) as server:
            # Activate the server; this will keep running until you
            # interrupt the program with Ctrl-C
            server.serve_forever()
            if mode == "Unix":
                try:
                    os.remove(connection)
                except OSError:
                    ...

    client_class = NodeCommClient
    @classmethod
    def client_request(cls, *args, client_class=None, connection=None):
        if client_class is None:
            client_class = cls.client_class
        if connection is None:
            connection = cls.DEFAULT_CONNECTION
        return client_class(connection).communicate(*args)

class ShellCommHandler(NodeCommHandler):

    @abc.abstractmethod
    def get_subprocess_call_list(self):
        ...

    def get_methods(self) -> 'dict[str,method]':
        return {
            k:self._wrap_subprocess_call(v)
            for k,v in self.get_subprocess_call_list()
        }

    def _wrap_subprocess_call(self, command):
        if isinstance(command, str):
            def command(*args, _cmd=command, **kwargs):
                return self.subprocess_response(_cmd, *args, **kwargs)
        elif not callable(command):
            def command(*args, _cmd=command, **kwargs):
                return self.subprocess_response(*_cmd, *args, **kwargs)
        return command
