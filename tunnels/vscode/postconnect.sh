#! /bin/bash

echo -e "\x1b[1;35m" >> $SESSION_FILE
echo "########################################################################" >> $SESSION_FILE
echo "##############          TUNNEL USAGE INSTRUCTIONS         ##############" >> $SESSION_FILE
echo "Open http://localhost:$HOST_PORT and supply the password listed below" >> $SESSION_FILE
cat ~/.config/code-server/config.yaml >> $SESSION_FILE
echo "In VS Code terminals, start by running \`source $TUNNEL_ENV_FIlE\`"  >> $SESSION_FILE
echo "If necessary, to load the same conda env, use \`conda activate $CONDA_ENVIRONMENT\`"  >> $SESSION_FILE
echo -e "\033[0m" >> $SESSION_FILE

tail -f -n +1 $SESSION_FILE
scancel $SESSION_ID