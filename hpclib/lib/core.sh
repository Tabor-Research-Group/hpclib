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
# FLAGS_PAT strings elsewhere in this file. Sets MC_FOUND/MC_TAKES_VALUE
# (plain globals, not "local -A") rather than returning, since this
# runs once per token in a hot loop.
function _mclong_lookup {
  local spec="$1" name="$2" tok
  MC_FOUND=false
  MC_TAKES_VALUE=false
  local IFS=','
  for tok in $spec; do
    if [ "$tok" = "$name" ]; then
      MC_FOUND=true; return
    elif [ "$tok" = "${name}:" ]; then
      MC_FOUND=true; MC_TAKES_VALUE=true; return
    fi
  done
}

# The core parser. A single left-to-right pass over "$@" that
# classifies each token by its ORIGINAL INDEX rather than bucketing
# into separate lists and concatenating them - bucket-then-concatenate
# is what destroys ordering for tools like `singularity run --wd=x
# img.sif --copt=1`.
#
# bash <4.3 has no namerefs ("local -n"), so instead of writing
# through caller-supplied array names, this always writes to a FIXED
# set of globals (MC_SHORT_STREAM etc. below). That's safe here
# specifically because none of mcopts/mcoptsfrom/mcoptvalue/mcargs
# ever call each other, or themselves, before finishing with these
# results - there's no reentrancy to worry about. Read the MC_* arrays
# immediately after calling this, before calling it again.
#
# bash <4.0 also has no associative arrays ("local -A"), so
# long-flag matches are stored as two PARALLEL indexed arrays
# (MC_LONG_NAMES / MC_LONG_VALUES) instead of one assoc array; see
# _mclong_get below for the corresponding lookup.
function _mcparse_long {
  local long_spec="$1"; shift

  MC_SHORT_STREAM=()
  MC_SHORT_INDEX=()
  MC_LONG_REM=()
  MC_LONG_REM_IDX=()
  MC_LONG_NAMES=()
  MC_LONG_VALUES=()

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
        if ! $MC_FOUND; then
          MC_LONG_REM+=("$tok")
          MC_LONG_REM_IDX+=("$i")
        elif ! $MC_TAKES_VALUE; then
          MC_LONG_NAMES+=("$key")
          MC_LONG_VALUES+=(true)
        elif $has_eq; then
          MC_LONG_NAMES+=("$key")
          MC_LONG_VALUES+=("$val")
        elif $MCLONGOPTS_IMPLICIT_ARGUMENTS && [ $((i+1)) -lt $n ]; then
          MC_LONG_NAMES+=("$key")
          MC_LONG_VALUES+=("${args[$((i+1))]}")
          i=$((i+1))                     # also consumes the value token
        else
          MC_LONG_NAMES+=("$key")
          MC_LONG_VALUES+=("")           # declared value-taking but no "="
        fi                               # and implicit-args off (or nothing left)
        ;;
      *)
        MC_SHORT_STREAM+=("$tok")
        MC_SHORT_INDEX+=("$i")
        ;;
    esac
    i=$((i+1))
  done
}

# Scans the MC_LONG_NAMES/MC_LONG_VALUES parallel arrays left behind by
# the most recent _mcparse_long call. This - not a "local -A" lookup -
# is the 3.2-compatible stand-in for hash-map access.
function _mclong_get {
  local name="$1"
  local i
  for i in "${!MC_LONG_NAMES[@]}"; do
    if [ "${MC_LONG_NAMES[$i]}" = "$name" ]; then
      printf "%s" "${MC_LONG_VALUES[$i]}"
      return 0
    fi
  done
  printf "%s" ""
  return 1
}

# mcopts: EXTRACT OPTIONS
#     Takes a flag pattern, long-flag spec, ignore pattern, and call
#     signature. Returns the short opts that DON'T match ignore_pat.

function mcopts {
  local flag_pat="$1"; shift
  local long_spec="$1"; shift
  local ignore_pat="$1"; shift
  local opt_string="" opt_whitespace opt OPTARG OPTIND=1

  _mcparse_long "$long_spec" "$@"
  set -- "${MC_SHORT_STREAM[@]}"

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
  local flag_pat="$1"; shift
  local long_spec="$1"; shift
  local ignore_pat="$1"; shift
  local opt_string="" opt_whitespace opt OPTARG OPTIND=1

  _mcparse_long "$long_spec" "$@"
  set -- "${MC_SHORT_STREAM[@]}"

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
# for the long-flag equivalent, and see mclongoptvalue below that for
# the difference between the two)
#     Takes a flag pattern, long-flag spec, opt key, and call
#     signature. Returns the opt value for the key.

function mcoptvalue {
  local flag_pat="$1"; shift
  local long_spec="$1"; shift
  local value_pat="$1"; shift
  local opt opt_string="" opt_whitespace OPTARG OPTIND=1

  _mcparse_long "$long_spec" "$@"
  set -- "${MC_SHORT_STREAM[@]}"

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
#     Takes a long-flag spec, a name, and a call signature. Returns
#     the value bound to that name by _mcparse_long (booleans read
#     back as "true"/"", per MCLONGOPTS_IMPLICIT_ARGUMENTS as
#     described above _mcparse_long). Use this - not mclongoptvalue -
#     whenever you're also calling mcopts/mcoptsfrom/mcargs/mcoptvalue
#     against the same argv and the same spec, so the whole argv is
#     parsed exactly once and consistently.
function mclongvalue {
  local long_spec="$1"; shift
  local name="$1"; shift
  _mcparse_long "$long_spec" "$@"
  _mclong_get "$name"
}

# mcargs: EXTRACT ARGUMENTS
#     Takes a flag pattern and long-flag spec, and a call signature.
#     Returns whatever's left after matched short AND long flags are
#     removed - in ORIGINAL ARGV ORDER, so interleaved positionals and
#     passthrough long options (declared or not) survive intact.

function mcargs {
  local flag_pat="$1"; shift
  local long_spec="$1"; shift
  local opt OPTARG OPTIND

  _mcparse_long "$long_spec" "$@"

  # getopts only ever sees the short-shaped subsequence, so it can
  # never be corrupted by a "--foo" token; it still stops at the first
  # non-option exactly as before, leaving that suffix unconsumed.
  OPTIND=1
  set -- "${MC_SHORT_STREAM[@]}"
  while getopts "$flag_pat" opt; do
      :
  done
  shift "$((OPTIND -1))";
  local short_unmatched=("$@")
  local n_matched_short=$(( ${#MC_SHORT_STREAM[@]} - ${#short_unmatched[@]} ))

  # Merge short_unmatched + MC_LONG_REM back into ONE list ordered by
  # original argv index. A plain INDEXED array (not "local -A") is
  # enough here - arbitrary non-negative integers are valid indexed-
  # array subscripts too, and bash guarantees "${!by_index[@]}" comes
  # back in ascending order for indexed arrays (it's only associative
  # arrays that have no order guarantee) - so no extra sort is needed.
  local by_index=()
  local j
  for ((j = 0; j < ${#short_unmatched[@]}; j++)); do
    by_index[${MC_SHORT_INDEX[$((n_matched_short + j))]}]="${short_unmatched[$j]}"
  done
  for ((j = 0; j < ${#MC_LONG_REM[@]}; j++)); do
    by_index[${MC_LONG_REM_IDX[$j]}]="${MC_LONG_REM[$j]}"
  done

  local key sorted=()
  for key in "${!by_index[@]}"; do
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

  printf "%s" "$*"
}