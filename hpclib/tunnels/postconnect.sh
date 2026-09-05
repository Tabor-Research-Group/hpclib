#! /bin/bash

tail -f -n +1 $SESSION_FILE
scancel $SESSION_ID