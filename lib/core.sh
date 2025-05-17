
## Base utilities
function string_array_index {
  local i;
  local arr=($1);
  local value=$2;

  for i in "${!arr[@]}"; do
    if [[ "${arr[$i]}" = "${value}" ]];
      then echo "${i}"
      break;
    fi
  done
}

function _get_user {
  local user="$1";

  if [ "$user" = "" ]
    then user=$MAIN_USER;
  fi;
  echo "$user"
}

function _build_argstr {
  local argstr=$1;
  local extra=$2;
  local flag=$3;

  if [ "$extra" != "" ]; then
    if [ "$flag" != "" ]; then
        if [ "$flag" = "--*" ]
          then extra="$flag=$extra"
          else  extra="$flag $extra"
        fi
    fi
  fi
  if [ "$extra" != "" ]; then
    if [ "$argstr" = "" ]
      then
        argstr="$extra"
      else
        argstr="$argstr $extra"
    fi
  fi

  echo "$argstr"
}

## Identifiers

function random_id {
  # thanks Stack Overflow
  local num_chars=$1;
  local rand_str;

  if [ "$num_chars" = "" ]
    then num_chars=13
  fi

  rand_str=$(tr -dc A-Za-z0-9 </dev/urandom | head -c $num_chars; echo);
  if [ "$rand_str" = "" ]
    then rand_str=$(openssl rand -base64 $num_chars)
  fi

  echo "$rand_str"
}

DEFAULT_MIN_PORT=8000
DEFAULT_MAX_PORT=40000
function random_port {
  local min_port=$1;
  local max_port=$2;
  local port

  if [ "$min_port" = "" ]; then
    min_port=$DEFAULT_MIN_PORT
  fi
  if [ "$max_port" = "" ]; then
    max_port=$DEFAULT_MAX_PORT
  fi

  port=$(shuf -i $min_port-$max_port -n 1)
  echo $port
}

############################################################################
############################# ARG PARSE STUFF ##############################
############################################################################

# mcopts: EXTRACT OPTIONS
#     Takes a flag pattern and call signature
#     Returns the opts

function mcopts {

  local flag_pat;
  local ignore_pat;
  local opt_string;
  local opt_whitespace;
  local opt;
  local OPTARG;
  local OPTIND=1;

  flag_pat="$1";
  shift
  ignore_pat="$1";
  shift

  while getopts "$flag_pat" opt; do
    if [[ "$opt" =~ $ignore_pat ]]
      then
        :
      else
        if [ "$opt_string" != "" ]
          then opt_whitespace=" ";
          else opt_whitespace="";
        fi;
        if [ "$OPTARG" != "" ]
          then opt_string="$opt_string$opt_whitespace-$opt $OPTARG"
          else opt_string="$opt_string$opt_whitespace-$opt"
        fi
    fi
  done

  printf "%s" "$opt_string"

}

# mcopts: EXTRACT OPTIONS
#     Takes a flag pattern, match pattern, and call signature
#     Returns the opts that match

function mcoptsfrom {

  local flag_pat;
  local match_path;
  local opt_string;
  local opt_whitespace;
  local opt;
  local OPTARG;
  local OPTIND=1;

  flag_pat="$1";
  shift
  ignore_pat="$1";
  shift

  while getopts "$flag_pat" opt; do
    if [[ "$opt" =~ $ignore_pat ]]
      then
        if [ "$opt_string" != "" ]
          then opt_whitespace=" ";
          else opt_whitespace="";
        fi;
        if [ "$OPTARG" != "" ]
          then opt_string="$opt_string$opt_whitespace-$opt $OPTARG"
          else opt_string="$opt_string$opt_whitespace-$opt"
        fi
    fi
  done

  printf "%s" "$opt_string"

}

# mcoptvalue: EXTRACT OPTION VALUE
#     Takes a flag pattern, opt key, and call signature
#     Returns the opt value for the key

function mcoptvalue {

  local flag_pat;
  local value_pat;
  local opt;
  local opt_string;
  local opt_whitespace;
  local OPTARG;
  local OPTIND=1;

  flag_pat="$1";
  shift
  value_pat="$1";
  shift

  # while getopts ":$flag_pat:" opt; do
  #   case "$opt" in
  #     $value_pat)
  #       if [ "$opt_string" != "" ]
  #         then opt_whitespace=" ";
  #         else opt_whitespace="";
  #       fi;
  #       if [ "$OPTARG" = "" ]
  #         then OPTARG=true;
  #       fi
  #       opt_string="$opt_string$opt_whitespace$OPTARG"
  #       ;;
  #   esac;
  # done

  OPTIND=1;

  if [ "$opt_string" == "" ]; then
    while getopts "$flag_pat" opt; do
      case "$opt" in
        $value_pat)
          if [ "$opt_string" != "" ]
            then opt_whitespace=" ";
            else opt_whitespace="";
          fi;
          if [ "$OPTARG" = "" ]
            then OPTARG=true;
          fi
          opt_string="$opt_string$opt_whitespace$OPTARG"
          ;;
      esac;
    done
  fi

  printf "%s" "$opt_string"
}

# mcargs: EXTRACT ARGUMENTS
#     Takes a flag pattern and call signature
#     Returns just the args

function mcargs {

  local flag_pat;
  local opt;
  local OPTARG;
  local OPTIND;

  flag_pat="$1";
  shift

  while getopts "$flag_pat" opt; do
      :
  done
  shift "$((OPTIND -1))";

  printf "%s" "$*"

}