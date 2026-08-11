#!/bin/bash

ADDRESS=<user>@<location>
TUNNEL=vscode

. ~/Documents/Postdoc/Development/hpclib/hpclib.sh
psync -r $HPCLIB_DIR/ $ADDRESS:hpclib/
launch_tunnel -b $ADDRESS $TUNNEL --mem=30GB