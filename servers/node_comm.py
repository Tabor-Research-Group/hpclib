"""
A simple handler for running subprocess calls on
different nodes in SLURM systems
"""
import abc
import os
import socket, socketserver, json, traceback, subprocess
import sys

__all__ = [
    "NodeCommHandler",
    "NodeCommClient"
]

def infer_mode(connection):
    if (
            isinstance(connection, tuple)
            and isinstance(connection[0], str) and isinstance(connection[1], int)
    ):
        mode = "IP"
    elif isinstance(connection, str):
        mode = "Unix"
    else:
        raise ValueError(f"invalid connection spec {connection}")
    return mode

class NodeCommClient:
    def __init__(self, connection, timeout=10):
        self.host = host
        self.port = port
        self.conn = connection
        mode = infer_mode(connection)
        if mode == "IP":
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
        with socket.socket(self.mode, socket.SOCK_STREAM) as sock:
            # Connect to server and send data
            sock.connect(self.conn)
            sock.settimeout(self.timeout)
            sock.sendall(request)
            # Receive data from the server and shut down
            body = sock.recv(1024)

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
            self.wfile.write(json.dumps(response).encode())
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
        # If you are using python 2.x, you need to include shell=True in the above line
        std_out, std_err = pipes.communicate()
        return {
            "stdout":std_out.strip().decode(),
            "stderr":std_err.strip().decode()
        }
    @abc.abstractmethod
    def get_methods(self) -> 'dict[str,method]':
        ...

    DEFAULT_CONNECTION = ("localhost", 9999)
    @classmethod
    def start_server(cls, connection=None):
        # Create the server, binding to localhost on port 9999
        if connection is None:
            connection = cls.DEFAULT_CONNECTION
        mode = infer_mode(connection)
        if mode == "IP":
            server_type = socketserver.TCPServer
        elif mode == "Unix":
            server_type = socketserver.UnixStreamServer
        else:
            raise NotImplementedError(mode)
        with server_type(mode, cls) as server:
            # Activate the server; this will keep running until you
            # interrupt the program with Ctrl-C
            server.serve_forever()

    client_class = NodeCommClient
    @classmethod
    def client_request(cls, *args, client_class=None, connection=None):
        if client_class is None:
            client_class = cls.client_class
        if connection is None:
            connection = cls.DEFAULT_CONNECTION
        return client_class(connection).communicate(*args)