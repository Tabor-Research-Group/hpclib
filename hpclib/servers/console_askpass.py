#!/usr/bin/env python3
"""
SSH_ASKPASS helper for agent_console's cluster login. ssh runs it once per
prompt (password, Duo), with the prompt text as its argument; it asks the
console over the console's private socket and prints the answer for ssh.

It only answers for the login attempt named by $HPCLIB_ASKPASS_NONCE, which
the console sets on the ssh it starts; anything else gets nothing back, and
ssh gives up. Standard library only.
"""
import json
import os
import socket
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path, nonce = os.environ.get("HPCLIB_ASKPASS_SOCKET"), os.environ.get("HPCLIB_ASKPASS_NONCE")
    if not path or not nonce:
        print("console_askpass: not started by agent_console", file=sys.stderr)
        return 1
    prompt = argv[0] if argv else ""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(330)   # the console decides how long a prompt may wait
            s.connect(path)
            s.sendall(json.dumps({"nonce": nonce, "prompt": prompt}).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
        answer = json.loads(reply or b"{}")
    except (OSError, ValueError) as e:
        print(f"console_askpass: no answer from agent_console ({e})", file=sys.stderr)
        return 1
    if "answer" not in answer:
        print(f"console_askpass: {answer.get('error', 'refused')}", file=sys.stderr)
        return 1
    sys.stdout.write(answer["answer"] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
