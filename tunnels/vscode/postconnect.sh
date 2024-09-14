#! /bin/bash

cat ~/.config/code-server/config.yaml >> $SESSION_FILE
tail -f $SESSION_FILE