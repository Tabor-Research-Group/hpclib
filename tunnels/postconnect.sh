#! /bin/bash

if [ -f $post_file ];
then source $post_file;
tail -f $SESSION_FILE