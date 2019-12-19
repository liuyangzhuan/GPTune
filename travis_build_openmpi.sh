#!/bin/bash

touch build_mpi.log
sh log_monitor.sh build_mpi.log &
make -j8 &>build_mpi.log
make install &>build_mpi.log
echo "this is finished" >> build_mpi.log
