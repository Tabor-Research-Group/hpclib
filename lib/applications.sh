

DEFAULT_GAUSSIAN_COMMAND=g16
RUN_GAUSSIAN_FLAGS="g:"
function run_gaussian {
  local gaussian_version=$(mcoptvalue "$RUN_GAUSSIAN_FLAGS" "g" $@)
  local input_file="$1"

  if [ -z "$gaussian_version" ]; then
    gaussian_version="$DEFAULT_GAUSSIAN_COMMAND"
  fi
  if [ -z "$gaussian_version" ]; then
    echo "no Gaussian binary specified"
    return 1
  fi

  if [ -z "$input_file"]; then
    echo "no Gaussian input file specified"
    return 1
  fi

  slurm_job_info
  slurm_command_execute -J "*.gjf" -R "*.chk,*.log" "$gaussian_version" "$input_file"
  slurm_job_footer
}