#!/bin/bash

touch build_python2.log
sh log_monitor.sh build_python2.log &
make -j8 &>build_python2.log
make install &>build_python2.log
echo "this is finished" >> build_python2.log
