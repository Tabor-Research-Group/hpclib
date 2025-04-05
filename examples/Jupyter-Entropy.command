#!/bin/bash

. ~/Documents/Postdoc/Development/hpclib/hpclib.sh
USER=maboyer
HOST=entropy.chem.tamu.edu
PORT=$(shuf -i 8000-40000 -n 1)
TUNNEL=jupyter

PS1="\u\@$HOST-$TUNNEL\$ "
PROMPT_COMMAND='echo -ne "\033]0;$HOST-$TUNNEL: ${PWD}\007"'

psync -r ~/Documents/Postdoc/Development/hpclib/ $USER@$HOST:hpclib
#open -a Safari http://localhost:$PORT
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --app=http://localhost:$PORT --profile-directory="Profile 6"
pssh -L 127.0.0.1:$PORT:127.0.0.1:$PORT $USER@$HOST "/bin/bash hpclib/tunnels/start_tunnel.sh ${TUNNEL} -P $PORT"