#!/usr/bin/env bash
echo " ============================"
echo "| Sourcing the setup scripts |"
echo " ============================"
source /opt/ros/foxy/setup.bash 
source /opt/intel/openvino_2021/bin/setupvars.sh
source ~/deepracer_ws/add_deepracer_ftl_test/deepracer_follow_the_leader_ws/install/setup.bash
echo ""
echo " ====================================="
echo "| Setting the “followtheleader” mode  |"
echo " ====================================="
ros2 service call /ctrl_pkg/vehicle_state deepracer_interfaces_pkg/srv/ActiveStateSrv "{state: 3}"
echo ""
echo " =================================="
echo "| Enabling “followtheleader” mode  |"
echo " =================================="
ros2 service call /ctrl_pkg/enable_state deepracer_interfaces_pkg/srv/EnableStateSrv "{is_active: True}"
