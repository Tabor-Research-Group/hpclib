"""
The Workspace server's parts. workspace_server.py keeps the HTTP side (the token, routing, static files, the signed file
URLs); each module here owns one kind of work and knows nothing of HTTP, so each can be tested and replaced alone:

  common      Refused (an answer with a status), resolve() for a path from the page, the data directory
  shellenv    the login shell's environment, captured once; run() a program with it, login() a shell command
  fs          the files API (list, stat, write, mkdir, rename, delete, usage, tail)       <- the file manager's
  terminal    detached terminal sessions (a pty and a ring buffer each) and the WebSocket framing
  cluster     partitions, GPU types, accounts and module names, as Slurm and `module` report them
  envs        the environment card and Prepare (uv and pixi), as the user, on the login node
  jobspec     the job spec, its validation, and the sbatch script made from it
  jobs        check, submit, the runs this Workspace made, their state, cancel
  profiles    named cluster/environment settings: last used, a project's .hpclib/job.json, and saved ones
  templates   the classic script templates (script_templates.py parses them)

Python 3.6 or newer, standard library only.
"""
