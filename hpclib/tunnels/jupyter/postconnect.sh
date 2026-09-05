#! /bin/bash

echo -e "\x1b[1;35m" >> $SESSION_FILE
echo "########################################################################" >> $SESSION_FILE
echo "##############          TUNNEL USAGE INSTRUCTIONS         ##############" >> $SESSION_FILE
echo "Open http://localhost:$HOST_PORT and supply the token listed above" >> $SESSION_FILE
echo "If necessary, to load the same conda env, use \`conda activate $CONDA_ENVIRONMENT\`"  >> $SESSION_FILE
echo -e "\033[0m" >> $SESSION_FILE

tail -f -n +1 $SESSION_FILE
scancel $SESSION_ID