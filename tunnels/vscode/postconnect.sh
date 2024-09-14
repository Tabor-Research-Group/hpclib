#! /bin/bash

echo "Open http://localhost:$HOST_PORT and supply the password listed below" >> $SESSION_FILE
cat ~/.config/code-server/config.yaml >> $SESSION_FILE
tail -f $SESSION_FILE