## Base utilities
function string_array_index {
  local i;
  local arr=($1);
  local value=$2;

  for i in "${!arr[@]}"; do
    if [[ "${arr[$i]}" = "${value}" ]];
      then printf "%s" "${i}"
      break;
    fi
  done
}

function _get_user {
  local user="$1";

  if [ "$user" = "" ]
    then user=$MAIN_USER;
  fi;
  printf "%s" "$user"
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

  printf "%s" "$argstr"
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

  printf "%s" "$rand_str"
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
  printf "%s" $port
}

############################################################################
############################# ARG PARSE STUFF ##############################
############################################################################

# Toggle: when true (default), a long flag declared as value-taking but
# given WITHOUT "=" consumes the next argv token as its value
# (`--sort %i`). When false, only "--sort=%i" is accepted; a bare
# "--sort" with no "=" is treated as present-but-empty.
MCLONGOPTS_IMPLICIT_ARGUMENTS=true

# Looks up $2 in the comma-separated long-spec $1, using the same
# "name" (boolean) / "name:" (takes a value) idiom as the short-flag
# FLAGS_PAT strings elsewhere in this file. Sets FOUND/TAKES_VALUE
# rather than returning, since this runs once per token in a hot loop.
function _mclong_lookup {
  local spec="$1" name="$2" tok
  FOUND=false
  TAKES_VALUE=false
  local IFS=','
  for tok in $spec; do
    if [ "$tok" = "$name" ]; then
      FOUND=true; return
    elif [ "$tok" = "${name}:" ]; then
      FOUND=true; TAKES_VALUE=true; return
    fi
  done
}

# The core fix. A single left-to-right pass over "$@" that classifies
# each token by its ORIGINAL INDEX rather than bucketing into separate
# lists and concatenating them - bucket-then-concatenate is what
# destroys ordering for tools like `singularity run --wd=x img.sif
# --copt=1`, where flags-before-the-positional and flags-after-it are
# semantically different and must never be reshuffled relative to each
# other or to the positionals between them.
#
# Long-form tokens ("--foo", "--foo=bar") that match long_spec are
# consumed into matched_long_ref (an associative array: name -> value,
# or name -> true for booleans). Everything else - short flags,
# positionals, undeclared "--foo" tokens, and a literal "--" - is left
# completely alone, recorded with its original index in
# short_stream_ref/short_index_ref (if it doesn't start with "--") or
# long_remainder_ref/remainder_index_ref (if it does). getopts is only
# ever run against short_stream_ref afterwards, so it can never be
# handed a "--"-prefixed token and never gets corrupted by one.
function _mcparse_long {
  local long_spec="$1"; shift
  local -n short_stream_ref=$1; shift
  local -n short_index_ref=$1; shift
  local -n long_remainder_ref=$1; shift
  local -n remainder_index_ref=$1; shift
  local -n matched_long_ref=$1; shift

  short_stream_ref=(); short_index_ref=()
  long_remainder_ref=(); remainder_index_ref=()
  matched_long_ref=()

  local args=("$@")
  local n=${#args[@]}
  local i=0 tok name key val has_eq

  while [ $i -lt $n ]; do
    tok="${args[$i]}"
    case "$tok" in
      --*)
        name="${tok#--}"                 # bare "--" -> name="" -> never found -> passthrough
        if [[ "$name" == *=* ]]; then
          key="${name%%=*}"; val="${name#*=}"; has_eq=true
        else
          key="$name"; has_eq=false
        fi

        _mclong_lookup "$long_spec" "$key"
        if ! $FOUND; then
          long_remainder_ref+=("$tok")
          remainder_index_ref+=("$i")
        elif ! $TAKES_VALUE; then
          matched_long_ref[$key]=true
        elif $has_eq; then
          matched_long_ref[$key]="$val"
        elif $MCLONGOPTS_IMPLICIT_ARGUMENTS && [ $((i+1)) -lt $n ]; then
          matched_long_ref[$key]="${args[$((i+1))]}"
          i=$((i+1))                     # also consumes the value token
        else
          matched_long_ref[$key]=""      # declared value-taking but no "="
        fi                               # and implicit-args off (or nothing left)
        ;;
      *)
        short_stream_ref+=("$tok")
        short_index_ref+=("$i")
        ;;
    esac
    i=$((i+1))
  done
}

# mcopts: EXTRACT OPTIONS
#     Takes a flag pattern, long-flag spec, ignore pattern, and call
#     signature. Returns the short opts that DON'T match ignore_pat.

function mcopts {

  local flag_pat;
  local long_spec;
  local ignore_pat;
  local opt_string;
  local opt_whitespace;
  local opt;
  local OPTARG;
  local OPTIND=1;
  local short_stream short_idx long_rem long_rem_idx;
  local -A long_matched;

  flag_pat="$1";
  shift
  long_spec="$1";
  shift
  ignore_pat="$1";
  shift

  _mcparse_long "$long_spec" short_stream short_idx long_rem long_rem_idx long_matched "$@"
  set -- "${short_stream[@]}"

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

# mcoptsfrom: EXTRACT OPTIONS
#     Takes a flag pattern, long-flag spec, match pattern, and call
#     signature. Returns the short opts that DO match match_pat.

function mcoptsfrom {

  local flag_pat;
  local long_spec;
  local match_path;
  local opt_string;
  local opt_whitespace;
  local opt;
  local OPTARG;
  local OPTIND=1;
  local short_stream short_idx long_rem long_rem_idx;
  local -A long_matched;

  flag_pat="$1";
  shift
  long_spec="$1";
  shift
  ignore_pat="$1";
  shift

  _mcparse_long "$long_spec" short_stream short_idx long_rem long_rem_idx long_matched "$@"
  set -- "${short_stream[@]}"

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

# mcoptvalue: EXTRACT OPTION VALUE (short flags only - see mclongvalue
# for the long-flag equivalent)
#     Takes a flag pattern, long-flag spec, opt key, and call
#     signature. Returns the opt value for the key.

function mcoptvalue {

  local flag_pat;
  local long_spec;
  local value_pat;
  local opt;
  local opt_string;
  local opt_whitespace;
  local OPTARG;
  local OPTIND=1;
  local short_stream short_idx long_rem long_rem_idx;
  local -A long_matched;

  flag_pat="$1";
  shift
  long_spec="$1";
  shift
  value_pat="$1";
  shift

  _mcparse_long "$long_spec" short_stream short_idx long_rem long_rem_idx long_matched "$@"
  set -- "${short_stream[@]}"

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

# mclongoptvalue: reads ONE specific long flag ("--name" / "--name=x")
# by scanning raw argv directly - it never touches getopts and has no
# notion of a declared spec, so it's the right tool for a quick,
# generic, one-off lookup ("does --resume appear anywhere, and what's
# its value") without having to declare a spec up front.
#
# mclongvalue (below) is the opposite: it requires a declared long_spec
# (boolean vs. value-taking) up front, same idiom as mcoptvalue for
# short flags, and is meant to be called alongside mcopts/mcargs/
# mcoptvalue against the SAME spec so all of a function's flags -
# short and long - are parsed exactly once, consistently, and
# INTERLEAVING-SAFE (mclongoptvalue's ad-hoc scan doesn't participate
# in the index-preserving merge mcargs performs, so mixing the two
# against the same argv can disagree about what's "consumed").
function mclongoptvalue {
  local target_opt="--$1"
  local opt_value="";
  local tmp_opt="";
  local test_opt="";
  shift
  while [ "${1#*--}" != "$1" ]; do
    case "$1" in
      --)
        break
        ;;
      *=*)
        tmp_opt=${1#*=}
        test_opt=${1%=*}
        ;;
      *)
        test_opt="$1"
        case "$2" in
          "")
            tmp_opt=true
            ;;
          --*)
            tmp_opt=true
            ;;
          *)
            shift
            tmp_opt="$1"
            ;;
        esac
    esac

    if [ "$test_opt" = "$target_opt" ]; then
      if [ -z "$tmp_opt" ]; then
        shift
        opt_value="$1"
      else
        opt_value="$tmp_opt"
      fi
      break
    fi
    shift
  done

  printf "%s" "$opt_value"
}

# mclongvalue: EXTRACT LONG OPTION VALUE
#     Takes a long-flag spec and a name, and a call signature. Returns
#     the value bound to that name by _mcparse_long (booleans read back
#     as "true"/"", per MCLONGOPTS_IMPLICIT_ARGUMENTS as described
#     above _mcparse_long). Use this - not mclongoptvalue - whenever
#     you're also calling mcopts/mcoptsfrom/mcargs/mcoptvalue against
#     the same argv and the same spec, so the whole argv is parsed
#     exactly once and consistently.
function mclongvalue {
  local long_spec="$1"; shift
  local name="$1"; shift
  local short_stream short_idx long_rem long_rem_idx
  local -A long_matched
  _mcparse_long "$long_spec" short_stream short_idx long_rem long_rem_idx long_matched "$@"
  printf "%s" "${long_matched[$name]:-}"
}

# mcargs: EXTRACT ARGUMENTS
#     Takes a flag pattern and long-flag spec, and a call signature.
#     Returns whatever's left after matched short AND long flags are
#     removed - in ORIGINAL ARGV ORDER, so interleaved positionals and
#     passthrough long options (declared or not) survive intact.

function mcargs {

  local flag_pat;
  local long_spec;
  local opt;
  local OPTARG;
  local OPTIND;
  local short_stream short_idx long_rem long_rem_idx;
  local -A long_matched;

  flag_pat="$1";
  shift
  long_spec="$1";
  shift

  _mcparse_long "$long_spec" short_stream short_idx long_rem long_rem_idx long_matched "$@"

  # getopts only ever sees the short-shaped subsequence, so it can
  # never be corrupted by a "--foo" token; it still stops at the first
  # non-option exactly as before, leaving that suffix unconsumed.
  OPTIND=1
  set -- "${short_stream[@]}"
  while getopts "$flag_pat" opt; do
      :
  done
  shift "$((OPTIND -1))";
  local short_unmatched=("$@")
  local n_matched_short=$(( ${#short_stream[@]} - ${#short_unmatched[@]} ))

  # Merge short_unmatched + long_rem back into ONE list ordered by
  # original argv index - this is what preserves interleaving across
  # both kinds of unmatched tokens.
  local -A by_index=()
  local j
  for ((j = 0; j < ${#short_unmatched[@]}; j++)); do
    by_index[${short_idx[$((n_matched_short + j))]}]="${short_unmatched[$j]}"
  done
  for ((j = 0; j < ${#long_rem[@]}; j++)); do
    by_index[${long_rem_idx[$j]}]="${long_rem[$j]}"
  done

  local key sorted=()
  for key in $(printf '%s\n' "${!by_index[@]}" | sort -n); do
    sorted+=("${by_index[$key]}")
  done
  printf "%s" "${sorted[*]}"
}

function mclongargs {
  while [ "${1#*--}" != "$1" ]; do
    case "$1" in
      --)
        shift
        break
        ;;
      *=*)
        ;;
      *)
        case "$2" in
          "")
            ;;
          --*)
            ;;
          *)
            shift
            ;;
        esac
    esac
    shift
  done

#  shift "$((OPTIND -1))";

  printf "%s" "$*"
}