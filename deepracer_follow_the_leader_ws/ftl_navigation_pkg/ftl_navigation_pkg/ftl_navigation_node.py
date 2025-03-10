#################################################################################
#   Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.          #
#                                                                               #
#   Licensed under the Apache License, Version 2.0 (the "License").             #
#   You may not use this file except in compliance with the License.            #
#   You may obtain a copy of the License at                                     #
#                                                                               #
#       http://www.apache.org/licenses/LICENSE-2.0                              #
#                                                                               #
#   Unless required by applicable law or agreed to in writing, software         #
#   distributed under the License is distributed on an "AS IS" BASIS,           #
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.    #
#   See the License for the specific language governing permissions and         #
#   limitations under the License.                                              #
#################################################################################

"""
ftl_navigation_node.py

This module decides the action messages (servo control messages specifically angle
and throttle) to be sent out using the detection deltas from object_detection_node.

The node defines:
    detection_delta_subscriber: A subscriber to the /object_detection_pkg/object_detection_delta
                                published by the object_detection_pkg with the normalized delta
                                of the detected object position from the target (reference) position
                                with respect to x and y axes.
    The node defines:
    action_publisher: A publisher to publish the action (angle and throttle values).
    set_max_speed_service: A service to dynamically set MAX_SPEED_PCT representing
                           the max speed percentage scale as per request.
"""
import time
import signal
import threading
import math
import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import (QoSProfile,
                       QoSHistoryPolicy,
                       QoSReliabilityPolicy)

from deepracer_interfaces_pkg.msg import (DetectionDeltaMsg,
                                          ServoCtrlMsg)
from deepracer_interfaces_pkg.srv import SetMaxSpeedSrv
from ftl_navigation_pkg import (constants,
                                utils, 
                                bmi160, 
                                deepracer_MPC)

import numpy as np


class FTLNavigationNode(Node):
    """Node responsible for deciding the action messages (servo control messages specifically angle
       and throttle) to be sent out using the detection deltas from object_detection_node.
    """

    def __init__(self, qos_profile):
        """Create a FTLNavigationNode.
        """
        super().__init__('ftl_navigation_node')
        self.get_logger().info("ftl_navigation_node started.")

        # Double buffer to hold the input deltas in x and y from Object Detection.
        self.delta_buffer = utils.DoubleBuffer(clear_data_on_get=True)

        # Create subscription to detection deltas from object_detection_node.
        self.detection_delta_subscriber = \
            self.create_subscription(DetectionDeltaMsg,
                                     constants.OBJECT_DETECTION_DELTA_TOPIC,
                                     self.detection_delta_cb,
                                     qos_profile)

        # Creating publisher to publish action (angle and throttle).
        self.action_publisher = self.create_publisher(ServoCtrlMsg,
                                                      constants.ACTION_PUBLISH_TOPIC,
                                                      qos_profile)

        # Service to dynamically set MAX_SPEED_PCT.
        self.set_max_speed_service = self.create_service(SetMaxSpeedSrv,
                                                         constants.SET_MAX_SPEED_SERVICE_NAME,
                                                         self.set_max_speed_cb)

        # Initializing the msg to be published.
        msg = ServoCtrlMsg()
        msg.angle, msg.throttle = constants.ActionValues.DEFAULT, constants.ActionValues.DEFAULT

        self.lock = threading.Lock()
        # Default maximum speed percentage (updated as per request using service call).
        self.max_speed_pct = constants.MAX_SPEED_PCT

        # Create a background servo publish thread.
        self.stop_thread = False
        self.thread_initialized = False
        self.thread = threading.Thread(target=self.action_publish, args=(msg,))
        self.thread.start()
        self.thread_initialized = True
        self.get_logger().info(f"Waiting for input delta: {constants.OBJECT_DETECTION_DELTA_TOPIC}")

        #-------------------------BEGIN ADDED CODE-------------------------
        # Create MPC controller and necessary variables
        self.MPC = deepracer_MPC.MPC()
        self.prev_ego_speed = 0
        self.start_time = time.time()
        self.prev_torque = 0
        self.f = 0.136768 #found a reference on learnopencv
        self.lamb = 8.387e-7
        #-------------------------END ADDED CODE-------------------------


    def wait_for_thread(self):
        """Function which joins the created background thread.
        """
        if self.thread_initialized:
            self.thread.join()
            self.get_logger().info("Thread joined")

    def thread_shutdown(self):
        """Function which sets the flag to shutdown background thread.
        """
        self.stop_thread = True

    def set_max_speed_cb(self, req, res):
        """Callback which dynamically sets the max_speed_pct.

        Args:
            req (SetMaxSpeedSrv.Request): Request object with the updated
                                          max speed percentage.
            res (SetMaxSpeedSrv.Response): Response object with error(int) flag
                                           indicating successful max speed pct
                                           update.

        Returns:
            SetMaxSpeedSrv.Response: Response object with error(int) flag indicating
                                             successful max speed pct update.

        """
        self.lock.acquire()
        try:
            self.max_speed_pct = req.max_speed_pct
            self.get_logger().info(f"Incoming request: max_speed_pct: {req.max_speed_pct}")
            res.error = 0
        except Exception as ex:
            self.get_logger().error(f"Failed set max speed pct: {ex}")
            res.error = 1
        finally:
            self.lock.release()
        return res

    def detection_delta_cb(self, detection_delta):
        """Call back for whenever detection delta for a perception
           is received from object_detection_node.

        Args:
            detection_delta (DetectionDeltaMsg): Message containing the normalized detection
                                                       delta in x and y axes respectively passed as
                                                       a list.
        """

        self.delta_buffer.put(detection_delta)

    #-------------------------BEGIN ADDED CODE-------------------------
    def get_imu_data(self): 
        accel_data = []
        gyro_data = []
        imu_dev = bmi160.accel_gyro_dev()
        accel_data,gyro_data = imu_dev.show_accel_gyro()
        return accel_data,gyro_data

    def normalize_neg_1_to_1(self,x, x_min, x_max):
        return 2*((x - x_min)/(x_max - x_min)) - 1

    # Estimates the distance to the front car
    # Use of simple optic geometry
    def get_front_distance(self, delta):
        delta_x, delta_y, _, _ = delta[0], delta[1]
        tilt = 20 *np.pi/180 # evaluation of camera tilt, need to do proper measurement
        f = 0.045333 # lens focal [meters]
        car_height = 0.135 # measurement in meters
        return np.cos(tilt)*f*(delta_x/car_height)/(delta_x/car_height-1)

    # Estimates the distance to the front car
    # Uses camera matrix
    def get_front_distance_camera_matrix(self, delta):
        _, _, bb_center_x, bb_center_y, target_x, target_y = delta[0], delta[1], delta[2], delta[3], delta[4], delta[5]
        Pi = np.array([[self.f,0,target_x,0],
                      [0,self.f,target_y,0],
                      [0,0,1,0]])
        relative_pos_vector  = np.linalg.pinv(Pi)@(np.array([[bb_center_x, bb_center_y,1]]).T)-np.array([[6.79995,4.5333,1.0,0.0]]).T
        distance = 0
        self.get_logger().info(f"Pseudo-inverse of C_cam: {relative_pos_vector}")
        for i in range(3):
            distance += relative_pos_vector[i]**2
        return self.lamb*distance

    # Simulate "phantom" front vehicle braking for a demo. 
    # Need car_dist as a parameter since it changes each time
    def get_sim_MPC_action(self, car_dist):
        # if first step of sim, set initial values 
        if self.prev_ego_speed == [0, 0, 0]:
            self.MPC.v_f = 1 # starting speed of "phamtom" front car in m/s

        # get current ego vehicle speed
        accel_data,gyro_data = self.get_imu_data()

        self.get_logger().info(f"Accelerometer data:{accel_data} gyro data: {gyro_data}")
        ego_speed = self.prev_ego_speed + accel_data[0]*0.1
        self.prev_ego_speed = ego_speed

        # construct state vector
        x_t = np.array([[car_dist],
                        [ego_speed]])

        self.get_logger().info(f"Before MPC step:{ego_speed}")
        
        # Step MPC with current states
        # [feas, x_opt, u_opt, J_opt] = self.MPC.MPC_step(x_t) #----------UNCOMMENT LATER ------------
        if feas != "infeasible":
            # if MPC finds a solution, use its torque output
            torque = u_opt.value[0][0]
        else:
            # if MPC can't find solution, use reduced previous torque 
            torque = self.prev_torque*0.9

        torque = 0.5 #----------REMOVE LATER------------
        
        self.prev_torque = torque
        self.get_logger().info(f"After MPC step:{ego_speed}")

        # calculate new distance between cars and slow down "phantom" front car
        car_dist += (self.MPC.v_f - ego_speed)*0.1
        time_elapsed = time.time() - self.start_time
        if time_elapsed > 20: # after 2 seconds, simulate slowing down "phantom" front car
            self.MPC.v_f = max(0, 1 - 0.1*(time_elapsed - 20)) # slow down by 0.1 m/s each second, clipped at 0 m/s

        # Convert MPC's output torque to throttle and update msg
        ########################
        #B: 0.00002*(x**2) + 0.0083*x + 11.461 RPM to PWM
        #A: y = -13.333x + 20000 RPM to Torque 
        #1. Calculate RPM from Torque from A
        #2. Calcualte PWM from RPM using B 
        #3. Use PWM as an input to servo node 
        #########################
        #rpm = (torque - 20000)/(-13.3333)
        #throttle = 0.00002*(rpm**2) + 0.0083*(rpm) + 11.461

        # Normalize torque betwen -1 and 1 to pass into get_rescaled_manual_speed
        throttle = self.normalize_neg_1_to_1(torque, self.MPC.torque_low, self.MPC.torque_high)
        throttle = self.get_rescaled_manual_speed(throttle , self.max_speed_pct)

        return throttle, car_dist
    #-------------------------END ADDED CODE-------------------------

    def plan_action(self, delta):
        """Helper method to calculate action to be undertaken from the detection delta
           received from object_detection_node.

        Args:
        delta (list of floats): detection deltas in x and y axes respectively.

        Returns:
        (int): Action Space Category defined in constants.py
        """
        delta_x = delta[0]
        delta_y = delta[1]

        if delta_y > constants.DeltaValueMap.REVERSE_DELTA_Y:
            # Reverse Bracket
            if delta_x <= constants.DeltaValueMap.REVERSE_RIGHT_DELTA_X:
                # Fast Right
                return constants.ACTION_SPACE[9][constants.ActionSpaceKeys.CATEGORY]
            elif delta_x >= constants.DeltaValueMap.REVERSE_LEFT_DELTA_X:
                # Fast Left
                return constants.ACTION_SPACE[8][constants.ActionSpaceKeys.CATEGORY]
            else:
                # No steering
                return constants.ACTION_SPACE[7][constants.ActionSpaceKeys.CATEGORY]

        elif delta_y <= constants.DeltaValueMap.REVERSE_DELTA_Y \
                and delta_y > constants.DeltaValueMap.FORWARD_DELTA_Y:
            # No Action Bracket
            return constants.ACTION_SPACE[1][constants.ActionSpaceKeys.CATEGORY]

        elif delta_y <= constants.DeltaValueMap.FORWARD_DELTA_Y:
            # Forward Bracket
            if delta_x < constants.DeltaValueMap.FORWARD_LEFT_DELTA_X \
                    and delta_x > constants.DeltaValueMap.FORWARD_FAST_LEFT_DELTA_X:
                # Slow Left
                return constants.ACTION_SPACE[3][constants.ActionSpaceKeys.CATEGORY]
            elif delta_x <= constants.DeltaValueMap.FORWARD_FAST_LEFT_DELTA_X:
                # Fast Left
                return constants.ACTION_SPACE[4][constants.ActionSpaceKeys.CATEGORY]
            elif delta_x > constants.DeltaValueMap.FORWARD_RIGHT_DELTA_X \
                    and delta_x < constants.DeltaValueMap.FORWARD_FAST_RIGHT_DELTA_X:
                # Slow Right
                return constants.ACTION_SPACE[5][constants.ActionSpaceKeys.CATEGORY]
            elif delta_x >= constants.DeltaValueMap.FORWARD_FAST_RIGHT_DELTA_X:
                # Fast Right
                return constants.ACTION_SPACE[6][constants.ActionSpaceKeys.CATEGORY]
            else:
                # No steering
                return constants.ACTION_SPACE[2][constants.ActionSpaceKeys.CATEGORY]

        else:
            # No Action
            return constants.ACTION_SPACE[1][constants.ActionSpaceKeys.CATEGORY]

    def get_mapped_action(self, action_category, max_speed_pct):
        """Return the angle and throttle values to be published for servo.

        Args:
            action_category (int): Integer value corresponding to the action space category.
            max_speed_pct (float): Float value ranging from 0.0 to 1.0 taken as input
                                   from maximum speed input.
        Returns:
            angle (float): Angle value to be published to servo.
            throttle (float): Throttle value to be published to servo.
        """
        action = constants.ACTION_SPACE[action_category][constants.ActionSpaceKeys.ACTION]
        self.get_logger().info(action)
        angle = constants.ACTION_SPACE[action_category][constants.ActionSpaceKeys.ANGLE]
        categorized_throttle = \
            constants.ACTION_SPACE[action_category][constants.ActionSpaceKeys.THROTTLE]
        throttle = self.get_rescaled_manual_speed(categorized_throttle, max_speed_pct)
        return angle, throttle

    def get_rescaled_manual_speed(self, categorized_throttle, max_speed_pct):
        """Return the non linearly rescaled speed value based on the max_speed_pct.

        Args:
            categorized_throttle (float): Float value ranging from -1.0 to 1.0.
            max_speed_pct (float): Float value ranging from 0.0 to 1.0 taken as input
                                   from maximum speed input.
        Returns:
            float: Categorized value of the input speed.
        """
        # return 0.0 if categorized_throttle or maximum speed pct is 0.0.
        if categorized_throttle == 0.0 or max_speed_pct == 0.0:
            return 0.0

        # get the parameter value to calculate the coefficients a, b in the equation y=ax^2+bx
        # The lower the update_speed_scale_value parameter, higher the impact on the
        # final mapped_speed.
        # Hence the update_speed_scale_value parameter is inversely associated with max_speed_pct
        # and bounded by MANUAL_SPEED_SCALE_BOUNDS.
        # Ex: max_speed_pct = 0.5; update_speed_scale_value = 3
        #     max_speed_pct = 1.0; update_speed_scale_value = 1
        # Lower the update_speed_scale_value: categorized_throttle value gets mapped to
        # higher possible values.
        #   Example: update_speed_scale_value = 1.0;
        #            categorized_throttle = 0.8 ==> mapped_speed = 0.992
        # Higher the update_speed_scale_value: categorized_throttle value gets mapped to
        # lower possible values.
        #   Example: update_speed_scale_value = 3.0;
        #            categorized_throttle = 0.8 ==> mapped_speed = 0.501

        inverse_max_speed_pct = (1 - max_speed_pct)
        update_speed_scale_value = \
            constants.MANUAL_SPEED_SCALE_BOUNDS[0] + \
            inverse_max_speed_pct * \
            (constants.MANUAL_SPEED_SCALE_BOUNDS[1] - constants.MANUAL_SPEED_SCALE_BOUNDS[0])
        speed_mapping_coefficients = dict()

        # recreate the mapping coefficients for the non-linear equation ax^2 + bx based on
        # the update_speed_scale_value.
        # These coefficents map the [update_speed_scale_value, update_speed_scale_value/2]
        # values to DEFAULT_SPEED_SCALE values [1.0, 0.8].
        speed_mapping_coefficients["a"] = \
            (1.0 / update_speed_scale_value**2) * \
            (2.0 * constants.DEFAULT_SPEED_SCALES[0] - 4.0 * constants.DEFAULT_SPEED_SCALES[1])
        speed_mapping_coefficients["b"] = \
            (1.0 / update_speed_scale_value) * \
            (4.0 * constants.DEFAULT_SPEED_SCALES[1] - constants.DEFAULT_SPEED_SCALES[0])
        return math.copysign(1.0, categorized_throttle) * \
            (speed_mapping_coefficients["a"] * abs(categorized_throttle)**2 +
             speed_mapping_coefficients["b"] * abs(categorized_throttle))

    def action_publish(self, msg):
        """Function which runs in a separate thread to read object detection delta
           from double buffer, decides the action and sends it to servo.

        Args:
            msg: detection_delta (DetectionDeltaMsg): Message containing the normalized
                 detection delta in x and y axes respectively passed as a list.
        """
        #-------------------------BEGIN ADDED CODE-------------------------
        sim_car_dist = 1 # for sim MPC
        #-------------------------END ADDED CODE-------------------------
        try:
            while not self.stop_thread:
                # Get a new message to plan action on
                detection_delta = self.delta_buffer.get()
                action_category = self.plan_action(detection_delta.delta)
                msg.angle, msg.throttle = self.get_mapped_action(action_category,
                                                                 self.max_speed_pct)

                #-------------------------BEGIN ADDED CODE-------------------------
                # Test front_distance function
                front_dist = self.get_front_distance_camera_matrix(detection_delta.delta)
                self.get_logger().info(f"Front Distance to Front Vehicle:{front_dist}")

                # Use sim MPC to calculate throttle
                msg.throttle, sim_car_dist = self.get_sim_MPC_action(sim_car_dist)
                #-------------------------END ADDED CODE-------------------------

                # Publish msg based on action planned and mapped from a new object detection.
                self.action_publisher.publish(msg)
                max_speed_pct = self.max_speed_pct

                # Sleep for a default amount of time before checking if new data is available.
                time.sleep(constants.DEFAULT_SLEEP)
                # If new data is not available within default time, gracefully run blind.
                while self.delta_buffer.is_empty() and not self.stop_thread:
                    # Decrease the max_speed_pct in every iteration so as to halt gradually.
                    max_speed_pct = max_speed_pct - 0.05
                    msg.angle, msg.throttle = self.get_mapped_action(action_category,
                                                                     max_speed_pct)
                    # Reducing angle value
                    msg.angle = msg.angle / 2

                    #-------------------------BEGIN ADDED CODE-------------------------
                    # Use sim MPC to calculate throttle
                    msg.throttle, sim_car_dist = self.get_sim_MPC_action(sim_car_dist)
                    #-------------------------END ADDED CODE-------------------------

                    # Publish blind action
                    self.action_publisher.publish(msg)
                    # Sleep before checking if new data is available.
                    time.sleep(0.1)
        except Exception as ex:
            self.get_logger().error(f"Failed to publish action to servo: {ex}")
            # Stop the car
            msg.angle, msg.throttle = constants.ActionValues.DEFAULT, constants.ActionValues.DEFAULT
            self.action_publisher.publish(msg)
            # Destroy the ROS Node running in another thread as well.
            self.destroy_node()
            rclpy.shutdown()


def main(args=None):
    rclpy.init(args=args)
    qos = QoSProfile(reliability=QoSReliabilityPolicy.RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT,
                     depth=1,
                     history=QoSHistoryPolicy.RMW_QOS_POLICY_HISTORY_KEEP_LAST)

    try:
        ftl_navigation_node = FTLNavigationNode(qos)
        executor = MultiThreadedExecutor()

        def signal_handler(signum, frame):
            """Callback function to handle registered signal handler
               to join and stop executing running thread created.
            Args:
                signum: The signal number.
                frame: the current stack frame (None or a frame object).
            """
            ftl_navigation_node.get_logger().info("Signal Handler initiated")
            ftl_navigation_node.thread_shutdown()
            ftl_navigation_node.wait_for_thread()

        # Register SIGINT handler
        signal.signal(signal.SIGINT, signal_handler)
        rclpy.spin(ftl_navigation_node, executor)
    except Exception as ex:
        ftl_navigation_node.get_logger().error(f"Exception in FTLNavigationNode: {ex}")
        ftl_navigation_node.destroy_node()
        rclpy.shutdown()

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    ftl_navigation_node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
