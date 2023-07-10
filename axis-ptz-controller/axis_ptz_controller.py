import ast
import coloredlogs
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from time import sleep, time
from types import FrameType
from typing import Any, Dict, Optional, Union

import numpy as np
import quaternion
import paho.mqtt.client as mqtt
import schedule
from sensecam_control.vapix_config import CameraConfiguration

from base_mqtt_pub_sub import BaseMQTTPubSub
import axis_ptz_utilities
from camera_control import CameraControl

STYLES = {
    "critical": {"bold": True, "color": "red"},
    "debug": {"color": "green"},
    "error": {"color": "red"},
    "info": {"color": "white"},
    "notice": {"color": "magenta"},
    "spam": {"color": "green", "faint": True},
    "success": {"bold": True, "color": "green"},
    "verbose": {"color": "blue"},
    "warning": {"color": "yellow"},
}
coloredlogs.install(
    level=logging.INFO,
    fmt="%(asctime)s.%(msecs)03d \033[0;90m%(levelname)-8s "
    ""
    "\033[0;36m%(filename)-18s%(lineno)3d\033[00m "
    "%(message)s",
    level_styles=STYLES,
)


class AxisPtzController(BaseMQTTPubSub):
    """Point the camera at an object using a proportional rate
    controller, and capture images while in track."""

    def __init__(
        self,
        camera_ip: str,
        camera_user: str,
        camera_password: str,
        config_topic: str,
        orientation_topic: str,
        object_topic: str,
        capture_topic: str,
        logger_topic: str,
        heartbeat_interval: int,
        lambda_t: float = 0.0,
        varphi_t: float = 0.0,
        h_t: float = 0.0,
        update_interval: float = 0.1,
        capture_interval: int = 2,
        capture_dir: str = ".",
        lead_time: float = 0.5,
        pan_gain: float = 0.2,
        pan_rate_min: float = 1.8,
        pan_rate_max: float = 150.0,
        tilt_gain: float = 0.2,
        tilt_rate_min: float = 1.8,
        tilt_rate_max: float = 150.0,
        focus_slope: float = 0.0006,
        focus_intercept: float = 54.0,
        focus_min: int = 5555,
        focus_max: int = 9999,
        jpeg_resolution: str = "1920x1080",
        jpeg_compression: int = 5,
        use_mqtt: bool = True,
        use_camera: bool = True,
        include_age: bool = True,
        log_to_mqtt: bool = False,
        **kwargs: Any,
    ):
        """Instantiate the PTZ controller by connecting to the camera
        and message broker, and initializing data attributes.

        Parameters
        ----------
        camera_ip: str
            Camera IP address
        camera_user: str
            Camera user name
        camera_password: str
            Camera user password
        config_topic: str
            MQTT topic for subscribing to configuration messages
        orientation_topic: str
            MQTT topic for subscribing to orientation messages
        object_topic: str
            MQTT topic for subscribing to object messages
        capture_topic: str
            MQTT topic for publising capture messages
        logger_topic: str
            MQTT topic for publishing logger messages
        heartbeat_interval: int
            Interval at which heartbeat message is to be published [s]
        lambda_t: float
            Tripod geodetic longitude [deg]
        varphi_t: float = 0.0,
            Tripod geodetic latitude [deg]
        h_t: float = 0.0,
            Tripod geodetic altitude [deg]
        update_interval: float
            Interval at which pointing of the camera is computed [s]
        capture_interval: int
            Interval at which the camera image is captured [s]
        capture_dir: str
            Directory in which to place captured images
        lead_time: float
            Lead time used when computing camera pointing to the
            object [s]
        pan_gain: float
            Proportional control gain for pan error [1/s]
        pan_rate_min: float
            Camera pan rate minimum [deg/s]
        pan_rate_max: float
            Camera pan rate maximum [deg/s]
        tilt_gain: float
            Proportional control gain for tilt error [1/s]
        tilt_rate_min: float
            Camera tilt rate minimum [deg/s]
        tilt_rate_max: float
            Camera tilt rate maximum [deg/s]
        focus_slope: float
            Focus slope from measurement [%/m]
        focus_intercept: float
            Focus intercept from measurement [%]
        focus_min: int
            Focus minimum from settings
        focus_max: int
             Focus maximum from settings
        jpeg_resolution: str
            Image capture resolution, for example, "1920x1080"
        jpeg_compression: int
            Image compression: 0 to 100
        use_mqtt: bool
            Flag to use MQTT, or not
        use_camera: bool
            Flag to use camera configuration and control, or not
        include_age: bool
            Flag to include object message age in lead time, or not
        log_to_mqtt: bool
            Flag to publish logger messages to MQTT, or not

        Returns
        -------
        AxisPtzController
        """
        # Parent class handles kwargs, including MQTT IP
        super().__init__(**kwargs)
        self.camera_ip = camera_ip
        self.camera_user = camera_user
        self.camera_password = camera_password
        self.config_topic = config_topic
        self.orientation_topic = orientation_topic
        self.object_topic = object_topic
        self.capture_topic = capture_topic
        self.logger_topic = logger_topic
        self.heartbeat_interval = heartbeat_interval
        self.lambda_t = lambda_t
        self.varphi_t = varphi_t
        self.h_t = h_t
        self.update_interval = update_interval
        self.capture_interval = capture_interval
        self.capture_dir = capture_dir
        self.lead_time = lead_time
        self.pan_gain = pan_gain
        self.pan_rate_min = pan_rate_min
        self.pan_rate_max = pan_rate_max
        self.tilt_gain = tilt_gain
        self.tilt_rate_min = tilt_rate_min
        self.tilt_rate_max = tilt_rate_max
        self.focus_slope = focus_slope
        self.focus_intercept = focus_intercept
        self.focus_min = focus_min
        self.focus_max = focus_max
        self.jpeg_resolution = jpeg_resolution
        self.jpeg_compression = jpeg_compression
        self.use_mqtt = use_mqtt
        self.use_camera = use_camera
        self.include_age = include_age
        self.log_to_mqtt = log_to_mqtt

        # Always construct camera configuration and control since
        # instantiation only assigns arguments
        logging.info("Constructing camera configuration and control")
        self.camera_configuration = CameraConfiguration(
            self.camera_ip, self.camera_user, self.camera_password
        )
        self.camera_control = CameraControl(
            self.camera_ip, self.camera_user, self.camera_password
        )

        # Connect MQTT client
        if self.use_mqtt:
            logging.info("Connecting MQTT client")
            self.connect_client()
            sleep(1)
            self.publish_registration("PTZ Controller Module Registration")

        # Object id, timestamp of object message and corresponding
        # object longitude, latitude, and altitude, and position and
        # velocity relative to the tripod in the camera fixed (rst)
        # coordinate system
        self.object_id = "NA"
        self.timestamp_o = 0.0  # [s]
        self.lambda_o = 0.0  # [deg]
        self.varphi_o = 0.0  # [deg]
        self.h_o = 0.0  # [m]
        self.r_rst_o_0_t = np.zeros((3,))  # [m/s]
        self.v_rst_o_0_t = np.zeros((3,))  # [m/s]

        # Tripod yaw, pitch, and roll angles
        self.alpha = 0.0  # [deg]
        self.beta = 0.0  # [deg]
        self.gamma = 0.0  # [deg]

        # Tripod yaw, pitch, and roll rotation quaternions
        self.q_alpha = quaternion.quaternion()
        self.q_beta = quaternion.quaternion()
        self.q_gamma = quaternion.quaternion()

        # Tripod position in the geocentric (XYZ) coordinate system
        self.r_XYZ_t = np.zeros((3,))

        # Compute orthogonal transformation matrix from geocentric
        # (XYZ) to topocentric (ENz) coordinates
        self.E_XYZ_to_ENz = np.zeros((3, 3))
        self.e_E_XYZ = np.zeros((3,))
        self.e_N_XYZ = np.zeros((3,))
        self.e_z_XYZ = np.zeros((3,))

        # Orthogonal transformation matrix from geocentric (XYZ) to
        # camera housing fixed (uvw) coordinates
        self.E_XYZ_to_uvw = np.zeros((3, 3))

        # Position and velocity in the topocentric (ENz) coordinate
        # system of the object relative to the tripod at time zero
        self.r_ENz_o_0_t = np.zeros((3,))
        self.v_ENz_o_0_t = np.zeros((3,))

        # Distance between the object and the tripod at time one
        self.distance3d = 0.0  # [m]

        # Object azimuth and elevation angles
        self.azm_o = 0.0  # [deg]
        self.elv_o = 0.0  # [deg]

        # Object pan and tilt angles
        self.rho_o = 0.0  # [deg]
        self.tau_o = 0.0  # [deg]

        # Pan, and tilt rotation quaternions
        self.q_row = quaternion.quaternion()
        self.q_tau = quaternion.quaternion()

        # Orthogonal transformation matrix from camera housing (uvw)
        # to camera fixed (rst) coordinates
        self.E_XYZ_to_rst = np.zeros((3, 3))

        # Object pan and tilt rates
        self.rho_dot_o = 0.0  # [deg/s]
        self.tau_dot_o = 0.0  # [deg/s]

        # Time of pointing update, camera pan and tilt angles, zoom,
        # and focus
        self.timestamp_c = 0.0  # [s]
        self.rho_c = 0.0  # [deg]
        self.tau_c = 0.0  # [deg]
        self.zoom = 2000  # 1 to 9999 [-]
        self.focus = 60  # 0 to 100 [%]

        # Camera pan and tilt rates
        self.rho_dot_c = 0.0  # [deg/s]
        self.tau_dot_c = 0.0  # [deg/s]

        # Camera pan and tilt rate differences
        self.delta_rho_dot_c = 0.0  # [deg/s]
        self.delta_tau_dot_c = 0.0  # [deg/s]

        # Capture boolean and last capture time
        self.do_capture = False
        self.capture_time = 0.0

        # Initialize tripod position in the geocentric (XYZ)
        # coordinate system, orthogonal transformation matrix from
        # geocentric (XYZ) to topocentric (ENz) coordinates, and East,
        # North, and zenith unit vectors
        config_msg = {
            "data": {
                "axis-ptz-controller": {
                    "lambda_t": self.lambda_t,
                    "varphi_t": self.varphi_t,
                    "h_t": self.h_t,
                }
            }
        }
        self._config_callback(None, None, config_msg)

        # Initialize the rotations from the geocentric (XYZ)
        # coordinate system to the camera housing fixed (uvw)
        # coordinate system
        # TODO: Fix key?
        orientation_msg = {
            "data": {
                "camera": {
                    "tripod_yaw": self.alpha,
                    "tripod_pitch": self.beta,
                    "tripod_roll": self.gamma,
                }
            }
        }
        self._orientation_callback(None, None, orientation_msg)

        # Initialize camera pointing
        if self.use_camera:
            logging.debug(f"Absolute move to pan: {self.rho_c}, and tilt: {self.tau_c}")
            self.camera_control.absolute_move(
                self.rho_c, self.tau_c, self.zoom, 50, self.focus
            )

        # Handle the interrupt and terminate signals
        signal.signal(signal.SIGINT, self._exit_handler)
        signal.signal(signal.SIGTERM, self._exit_handler)

        # Log configuration parameters
        logging.info(
            f"""AxisPtzController initialized with parameters:

    camera_ip = {camera_ip}
    camera_user = {camera_user}
    camera_password = {camera_password}
    config_topic = {config_topic}
    orientation_topic = {orientation_topic}
    object_topic = {object_topic}
    capture_topic = {capture_topic}
    logger_topic = {logger_topic}
    heartbeat_interval = {heartbeat_interval}
    lambda_t = {lambda_t}
    varphi_t = {varphi_t}
    h_t = {h_t}
    update_interval = {update_interval}
    capture_interval = {capture_interval}
    capture_dir = {capture_dir}
    lead_time = {lead_time}
    pan_gain = {pan_gain}
    pan_rate_min = {pan_rate_min}
    pan_rate_max = {pan_rate_max}
    tilt_gain = {tilt_gain}
    tilt_rate_min = {tilt_rate_min}
    tilt_rate_max = {tilt_rate_max}
    focus_slope = {focus_slope}
    focus_intercept = {focus_intercept}
    focus_min = {focus_min}
    focus_max = {focus_max}
    jpeg_resolution = {jpeg_resolution}
    jpeg_compression = {jpeg_compression}
    use_mqtt = {use_mqtt}
    use_camera = {use_camera}
    include_age = {include_age}
    log_to_mqtt = {log_to_mqtt}
            """
        )

    def decode_payload(self, payload: mqtt.MQTTMessage) -> Dict[Any, Any]:
        """
        Decode the payload carried by a message.

        Parameters
        ----------
        payload: mqtt.MQTTMessage
            The MQTT message

        Returns
        -------
        data : Dict[Any, Any]
            The data component of the payload
        """
        # TODO: Establish and use message format convention
        content = json.loads(str(payload.decode("utf-8")))
        if "data" in content:
            data = content["data"]
        else:
            data = content
        return data

    def _config_callback(
        self,
        _client: Union[mqtt.Client, None],
        _userdata: Union[Dict[Any, Any], None],
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]],
    ) -> None:
        """
        Process configuration message.

        Parameters
        ----------
        _client: Union[mqtt.Client, None]
            MQTT client
        _userdata: Union[Dict[Any, Any], None]
            Any required user data
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]]
            An MQTT message, or dictionary

        Returns
        -------
        None
        """
        # Assign data attributes allowed to change during operation,
        # ignoring config message data without a "camera" key
        if type(msg) == mqtt.MQTTMessage:
            data = self.decode_payload(msg.payload)
        else:
            data = msg["data"]
        if "axis-ptz-controller" not in data:
            return
        logging.info(f"Processing config msg data: {data}")
        # TODO: Use module specific key?
        config = data["axis-ptz-controller"]
        self.lambda_t = config.get("tripod_longitude", self.lambda_t)  # [deg]
        self.varphi_t = config.get("tripod_latitude", self.varphi_t)  # [deg]
        self.h_t = config.get("tripod_altitude", self.h_t)  # [m]
        self.update_interval = config.get(
            "update_interval", self.update_interval
        )  # [s]
        self.capture_interval = config.get(
            "capture_interval", self.capture_interval
        )  # [s]
        self.capture_dir = config.get("capture_dir", self.capture_dir)
        self.lead_time = config.get("lead_time", self.lead_time)  # [s]
        self.zoom = config.get("zoom", self.zoom)  # [0-9999]
        self.pan_gain = config.get("pan_gain", self.pan_gain)  # [1/s]
        self.tilt_gain = config.get("tilt_gain", self.tilt_gain)  # [1/s]
        self.include_age = config.get("include_age", self.include_age)
        self.log_to_mqtt = config.get("log_to_mqtt", self.log_to_mqtt)

        # Compute tripod position in the geocentric (XYZ) coordinate
        # system
        self.r_XYZ_t = axis_ptz_utilities.compute_r_XYZ(
            self.lambda_t, self.varphi_t, self.h_t
        )

        # Compute orthogonal transformation matrix from geocentric
        # (XYZ) to topocentric (ENz) coordinates
        (
            self.E_XYZ_to_ENz,
            self.e_E_XYZ,
            self.e_N_XYZ,
            self.e_z_XYZ,
        ) = axis_ptz_utilities.compute_E_XYZ_to_ENz(self.lambda_t, self.varphi_t)

    def _orientation_callback(
        self,
        _client: Union[mqtt.Client, None],
        _userdata: Union[Dict[Any, Any], None],
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]],
    ) -> None:
        """
        Process orientation message.

        Parameters
        ----------
        _client: Union[mqtt.Client, None]
            MQTT client
        _userdata: Union[Dict[Any, Any], None]
            Any required user data
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]]
            An MQTT message, or dictionary

        Returns
        -------
        None
        """
        # Assign camera housing rotation angles
        if type(msg) == mqtt.MQTTMessage:
            data = self.decode_payload(msg.payload)
        else:
            data = msg["data"]
        logging.info(f"Processing orientation msg data: {data}")
        camera = data["camera"]
        self.alpha = camera["tripod_yaw"]  # [deg]
        self.beta = camera["tripod_pitch"]  # [deg]
        self.gamma = camera["tripod_roll"]  # [deg]

        # Compute the rotations from the geocentric (XYZ) coordinate
        # system to the camera housing fixed (uvw) coordinate system
        logging.debug(f"Initial E_XYZ_to_uvw: {self.E_XYZ_to_uvw}")
        (
            self.q_alpha,
            self.q_beta,
            self.q_gamma,
            self.E_XYZ_to_uvw,
            _,
            _,
            _,
        ) = axis_ptz_utilities.compute_camera_rotations(
            self.e_E_XYZ,
            self.e_N_XYZ,
            self.e_z_XYZ,
            self.alpha,
            self.beta,
            self.gamma,
            self.rho_c,
            self.tau_c,
        )
        logging.debug(f"Final E_XYZ_to_uvw: {self.E_XYZ_to_uvw}")

    def _object_callback(
        self,
        _client: Union[mqtt.Client, None],
        _userdata: Union[Dict[Any, Any], None],
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]],
    ) -> None:
        """
        Process object message.

        Parameters
        ----------
        _client: Union[mqtt.Client, None]
            MQTT client
        _userdata: Union[Dict[Any, Any], None]
            Any required user data
        msg: Union[mqtt.MQTTMessage, Dict[Any, Any]]
            An MQTT message, or dictionary

        Returns
        -------
        None

        """
        # Assign identifier, time, position, and velocity of the
        # object
        if type(msg) == mqtt.MQTTMessage:
            data = json.loads(self.decode_payload(msg.payload)["Selected Object"])
        else:
            data = msg["data"]
        if not set(
            [
                "object_id",
                "object_type",
                "timestamp",
                "latitude",
                "longitude",
                "altitude",
                "track",
                "horizontal_velocity",
                "vertical_velocity",
            ]
        ) <= set(data.keys()):
            logging.info(f"Required keys missing from object message data: {data}")
            return
        logging.info(f"Processing object msg data: {data}")
        self.timestamp_o = float(data["timestamp"])  # [s]
        self.timestamp_c = self.timestamp_o
        self.lambda_o = data["longitude"]  # [deg]
        self.varphi_o = data["latitude"]  # [deg]
        self.h_o = data["altitude"]  # [m]
        track_o = data["track"]  # [deg]
        ground_speed_o = data["horizontal_velocity"]  # [m/s]
        vertical_rate_o = data["vertical_velocity"]  # [m/s]

        # Compute position in the geocentric (XYZ) coordinate system
        # of the object relative to the tripod at time zero, the
        # observation time
        r_XYZ_o_0 = axis_ptz_utilities.compute_r_XYZ(
            self.lambda_o, self.varphi_o, self.h_o
        )
        r_XYZ_o_0_t = r_XYZ_o_0 - self.r_XYZ_t

        # Assign lead time, computing and adding age of object
        # message, if enabled
        lead_time = self.lead_time  # [s]
        if self.include_age:
            object_msg_age = datetime.utcnow().timestamp() - self.timestamp_o  # [s]
            logging.debug(f"Object msg age: {object_msg_age} [s]")
            lead_time += object_msg_age
        logging.info(f"Using lead time: {lead_time} [s]")

        # Compute position and velocity in the topocentric (ENz)
        # coordinate system of the object relative to the tripod at
        # time zero, and position at slightly later time one
        self.r_ENz_o_0_t = np.matmul(self.E_XYZ_to_ENz, r_XYZ_o_0_t)
        track_o = math.radians(track_o)
        self.v_ENz_o_0_t = np.array(
            [
                ground_speed_o * math.sin(track_o),
                ground_speed_o * math.cos(track_o),
                vertical_rate_o,
            ]
        )
        r_ENz_o_1_t = self.r_ENz_o_0_t + self.v_ENz_o_0_t * lead_time

        # Compute position, at time one, and velocity, at time zero,
        # in the geocentric (XYZ) coordinate system of the object
        # relative to the tripod
        r_XYZ_o_1_t = np.matmul(self.E_XYZ_to_ENz.transpose(), r_ENz_o_1_t)
        v_XYZ_o_0_t = np.matmul(self.E_XYZ_to_ENz.transpose(), self.v_ENz_o_0_t)

        # Compute the distance between the object and the tripod at
        # time one
        self.distance3d = axis_ptz_utilities.norm(r_ENz_o_1_t)

        # TODO: Restore?
        # Compute the distance between the object and the tripod
        # along the surface of a spherical Earth
        # distance2d = axis_ptz_utilities.compute_great_circle_distance(
        #     self.self.lambda_t,
        #     self.varphi_t,
        #     self.lambda_o,
        #     self.varphi_o,
        # )  # [m]

        # Compute the object azimuth and elevation relative to the
        # tripod
        self.azm_o = math.degrees(math.atan2(r_ENz_o_1_t[0], r_ENz_o_1_t[1]))  # [deg]
        self.elv_o = math.degrees(
            math.atan2(r_ENz_o_1_t[2], axis_ptz_utilities.norm(r_ENz_o_1_t[0:2]))
        )  # [deg]
        logging.debug(f"Object azimuth and elevation: {self.azm_o}, {self.elv_o} [deg]")

        # Compute pan and tilt to point the camera at the object
        r_uvw_o_1_t = np.matmul(self.E_XYZ_to_uvw, r_XYZ_o_1_t)
        self.rho_o = math.degrees(math.atan2(r_uvw_o_1_t[0], r_uvw_o_1_t[1]))  # [deg]
        self.tau_o = math.degrees(
            math.atan2(r_uvw_o_1_t[2], axis_ptz_utilities.norm(r_uvw_o_1_t[0:2]))
        )  # [deg]
        logging.debug(f"Object pan and tilt: {self.rho_o}, {self.tau_o} [deg]")
        object_id = data["object_id"]
        if self.use_camera and self.tau_o < 0:
            logging.info(f"Stopping image capture of object: {object_id}")
            self.do_capture = False
            logging.info("Stopping continuous pan and tilt")
            self.camera_control.stop_move()

        if self.use_camera:
            # Get camera pan and tilt
            self.rho_c, self.tau_c, _zoom, _focus = self.camera_control.get_ptz()
            logging.debug(f"Camera pan and tilt: {self.rho_c}, {self.tau_c} [deg]")

            # Point the camera at any new object directly
            if self.object_id != object_id:
                self.object_id = object_id
                logging.info(
                    f"Absolute move to pan: {self.rho_o}, and tilt: {self.tau_o}, with zoom: {self.zoom}, and focus: {self.focus}"
                )
                self.camera_control.absolute_move(
                    self.rho_o, self.tau_o, self.zoom, 50, self.focus
                )
                duration = max(
                    math.fabs(self.rho_c - self.rho_o) / (self.pan_rate_max / 2),
                    math.fabs(self.tau_c - self.tau_o) / (self.tilt_rate_max / 2),
                )
                logging.info(f"Sleeping: {duration} [s]")
                sleep(duration)
                return

        else:
            logging.debug(f"Controller pan and tilt: {self.rho_c}, {self.tau_c} [deg]")

        # Compute slew rate differences
        self.delta_rho_dot_c = self.pan_gain * (self.rho_o - self.rho_c)
        self.delta_tau_dot_c = self.tilt_gain * (self.tau_o - self.tau_c)
        logging.debug(
            f"Delta pan and tilt rates: {self.delta_rho_dot_c}, {self.delta_tau_dot_c} [deg/s]"
        )

        # Compute position and velocity in the camera fixed (rst)
        # coordinate system of the object relative to the tripod at
        # time zero after pointing the camera at the object
        (
            _,
            _,
            _,
            _,
            _,
            _,
            self.E_XYZ_to_rst,
        ) = axis_ptz_utilities.compute_camera_rotations(
            self.e_E_XYZ,
            self.e_N_XYZ,
            self.e_z_XYZ,
            self.alpha,
            self.beta,
            self.gamma,
            self.rho_o,
            self.tau_o,
        )
        self.r_rst_o_0_t = np.matmul(self.E_XYZ_to_rst, r_XYZ_o_0_t)
        self.v_rst_o_0_t = np.matmul(self.E_XYZ_to_rst, v_XYZ_o_0_t)

        # Compute object slew rate
        omega = (
            axis_ptz_utilities.cross(self.r_rst_o_0_t, self.v_rst_o_0_t)
            / axis_ptz_utilities.norm(self.r_rst_o_0_t) ** 2
        )
        self.rho_dot_o = math.degrees(-omega[2])
        self.tau_dot_o = math.degrees(omega[0])
        logging.debug(
            f"Object pan and tilt rates: {self.rho_dot_o}, {self.tau_dot_o} [deg/s]"
        )

        # Update camera pan and tilt rate
        self.rho_dot_c = self.rho_dot_o + self.delta_rho_dot_c
        self.tau_dot_c = self.tau_dot_o + self.delta_tau_dot_c
        logging.info(
            f"Camera pan and tilt rates: {self.rho_dot_c}, {self.tau_dot_c} [deg/s]"
        )

        # Compute and set focus, command camera pan and tilt rates,
        # and begin capturing images, if needed
        if self.use_camera:
            self.focus = int(
                (self.focus_max - self.focus_min)
                * (self.focus_slope * self.distance3d + self.focus_intercept)
                / 100.0
                + self.focus_min
            )  # [%]
            logging.debug(f"Commanding focus: {self.focus}")
            self.camera_control.set_focus(self.focus)

            pan_rate_index = self._compute_pan_rate_index(self.rho_dot_c)
            tilt_rate_index = self._compute_tilt_rate_index(self.tau_dot_c)
            logging.debug(
                f"Commanding pan and tilt rate indexes: {pan_rate_index}, {tilt_rate_index}"
            )
            self.camera_control.continuous_move(
                pan_rate_index,
                tilt_rate_index,
                0.0,
            )
            if not self.do_capture:
                logging.info(f"Starting image capture of object: {self.object_id}")
                self.do_capture = True
                self.capture_time = time()

        # Log camera pointing using MQTT
        if self.log_to_mqtt:
            msg = {
                "timestamp": datetime.utcnow().timestamp(),
                "data": {
                    "camera-pointing": {
                        "timestamp_c": self.timestamp_c,
                        "rho_o": self.rho_o,
                        "tau_o": self.tau_o,
                        "rho_dot_o": self.rho_dot_o,
                        "tau_dot_o": self.tau_dot_o,
                        "rho_c": self.rho_c,
                        "tau_c": self.tau_c,
                        "rho_dot_c": self.rho_dot_c,
                        "tau_dot_c": self.tau_dot_c,
                    }
                },
            }
            logging.debug(f"Publishing logger msg: {msg}")
            self.publish_to_topic(self.logger_topic, json.dumps(msg))

    def _compute_pan_rate_index(self, rho_dot: float) -> int:
        """Compute pan rate index between -100 and 100 using rates in
        deg/s, limiting the results to the specified minimum and
        maximum. Note that the dead zone from -1.8 to 1.8 deg/s is ignored.

        Parameters
        ----------
        rho_dot : float
            Pan rate [deg/s]

        Returns
        -------
        pan_rate : int
            Pan rate index
        """
        if rho_dot < -self.pan_rate_max:
            pan_rate = -100

        elif self.pan_rate_max < rho_dot:
            pan_rate = +100

        else:
            pan_rate = round((100 / self.pan_rate_max) * rho_dot)
        return pan_rate

    def _compute_tilt_rate_index(self, tau_dot: float) -> int:
        """Compute tilt rate index between -100 and 100 using rates in
        deg/s, limiting the results to the specified minimum and
        maximum. Note that the dead zone from -1.8 to 1.8 deg/s is ignored.

        Parameters
        ----------
        tau_dot : float
            Tilt rate [deg/s]

        Returns
        -------
        tilt_rate : int
            Tilt rate index
        """
        if tau_dot < -self.tilt_rate_max:
            tilt_rate = -100

        elif self.tilt_rate_max < tau_dot:
            tilt_rate = +100

        else:
            tilt_rate = round((100 / self.tilt_rate_max) * tau_dot)
        return tilt_rate

    def _capture_image(self) -> None:
        """When enabled, capture an image in JPEG format, and publish
        corresponding image metadata.

        Parameters
        ----------
        None

        Returns
        -------
        None
        """
        if self.do_capture:
            # Capture an image in JPEG format
            self.capture_time = time()
            datetime_c = datetime.utcnow()
            timestr = datetime_c.strftime("%Y-%m-%d-%H-%M-%S")
            image_filepath = Path(self.capture_dir) / "{}_{}_{}_{}_{}.jpg".format(
                self.object_id,
                int(self.azm_o) % 360,
                int(self.elv_o),
                int(self.distance3d),
                timestr,
            )
            logging.info(
                f"Capturing image of object: {self.object_id}, at: {self.capture_time}, in: {image_filepath}"
            )
            with tempfile.TemporaryDirectory() as d:
                with axis_ptz_utilities.pushd(d):
                    self.camera_configuration.get_jpeg_request(
                        camera=1,
                        resolution=self.jpeg_resolution,
                        compression=self.jpeg_compression,
                    )
                    shutil.move(list(Path(d).glob("*.jpg"))[0], image_filepath)

            # Populate and publish image metadata, getting current pan
            # and tilt, and accounting for object message age relative
            # to the image capture
            rho_c, tau_c, _zoom, _focus = self.camera_control.get_ptz()
            object_msg_age = datetime_c.timestamp() - self.timestamp_o  # [s]
            image_metadata = {
                "timestamp": timestr,
                "imagefile": str(image_filepath),
                "camera": {
                    "rho_c": rho_c,
                    "tau_c": tau_c,
                    "lambda_t": self.lambda_t,
                    "varphi_t": self.varphi_t,
                    "zoom": self.zoom,
                },
                "object": {
                    "object_msg_age": object_msg_age,
                    "r_ENz_o_0_t": self.r_ENz_o_0_t.tolist(),
                    "v_ENz_o_0_t": self.v_ENz_o_0_t.tolist(),
                },
            }
            logging.debug(
                f"Publishing metadata: {image_metadata}, for object: {self.object_id}, at: {self.capture_time}"
            )
            self.publish_to_topic(self.capture_topic, json.dumps(image_metadata))

    def _update_pointing(self) -> None:
        """Update values of camera pan and tilt using current pan and
        tilt rate. Note that these value likely differ slightly from
        the actual camera pan and tilt angles, and will be overwritten
        when processing a object message. The values are used for
        development and testing.

        Parameters
        ----------
        None

        Returns
        -------
        None
        """
        self.timestamp_c += self.update_interval
        self.rho_c += self.rho_dot_c * self.update_interval
        self.tau_c += self.tau_dot_c * self.update_interval

    def _exit_handler(self, signum: int, frame: Optional[FrameType]) -> None:
        """Exit the controller gracefully by stopping continuous pan
        and tilt.

        Parameters
        ----------
        signum : int
            The signal number

        frame : Optional[FrameType]
            The current stack frame (None or a frame object)

        Returns
        -------
        None
        """
        if self.use_camera:
            logging.info("Stopping continuous pan and tilt")
            self.camera_control.stop_move()
        logging.info("Exiting")
        sys.exit()

    def main(self) -> None:
        """Schedule module heartbeat and image capture, subscribe to
        all required topics, then loop forever. Update pointing for
        logging, and command zero camera pan and tilt rates and stop
        capturing images after twice the capture interval has elapsed.
        """
        if self.use_mqtt:
            # Schedule module heartbeat
            heartbeat_job = schedule.every(self.heartbeat_interval).seconds.do(
                self.publish_heartbeat, payload="PTZ Controller Module Heartbeat"
            )

            # Subscribe to required topics
            self.add_subscribe_topic(self.config_topic, self._config_callback)
            self.add_subscribe_topic(self.orientation_topic, self._orientation_callback)
            self.add_subscribe_topic(self.object_topic, self._object_callback)

        if self.use_camera:
            # Schedule image capture
            capture_job = schedule.every(self.capture_interval).seconds.do(
                self._capture_image
            )

        # Enter the main loop
        while True:
            try:
                # Run pending scheduled messages
                if self.use_mqtt:
                    schedule.run_pending()

                # Update camera pointing
                sleep(self.update_interval)
                if not self.use_camera:
                    self._update_pointing()

                # Command zero camera pan and tilt rates, and stop
                # capturing images if a object message has not been
                # received in twice the capture interval
                if (
                    self.do_capture
                    and time() - self.capture_time > 2.0 * self.capture_interval
                ):
                    logging.info(f"Stopping image capture of object: {self.object_id}")
                    self.do_capture = False
                    if self.use_camera:
                        logging.info("Stopping continuous pan and tilt")
                        self.camera_control.stop_move()

            except Exception as e:
                logging.error(f"Main loop exception: {e}")
                break


def make_controller() -> AxisPtzController:
    return AxisPtzController(
        camera_ip=os.getenv("CAMERA_IP", ""),
        camera_user=os.getenv("CAMERA_USER", ""),
        camera_password=os.getenv("CAMERA_PASSWORD", ""),
        mqtt_ip=os.getenv("MQTT_IP"),
        config_topic=os.getenv("CONFIG_TOPIC", ""),
        orientation_topic=os.getenv("ORIENTATION_TOPIC", ""),
        object_topic=os.getenv("OBJECT_TOPIC", ""),
        capture_topic=os.getenv("CAPTURE_TOPIC", ""),
        logger_topic=os.getenv("LOGGER_TOPIC", ""),
        heartbeat_interval=int(os.getenv("HEARTBEAT_INTERVAL", 10)),
        lambda_t=float(os.getenv("TRIPOD_LONGITUDE", 0.0)),
        varphi_t=float(os.getenv("TRIPOD_LATITUDE", 0.0)),
        h_t=float(os.getenv("TRIPOD_ALTITUDE", 0.0)),
        update_interval=float(os.getenv("UPDATE_INTERVAL", 0.1)),
        capture_interval=int(os.getenv("CAPTURE_INTERVAL", 2)),
        capture_dir=os.getenv("CAPTURE_DIR", "."),
        lead_time=float(os.getenv("LEAD_TIME", 0.5)),
        pan_gain=float(os.getenv("PAN_GAIN", 0.2)),
        pan_rate_min=float(os.getenv("PAN_RATE_MIN", 1.8)),
        pan_rate_max=float(os.getenv("PAN_RATE_MAX", 150.0)),
        tilt_gain=float(os.getenv("TILT_GAIN", 0.2)),
        tilt_rate_min=float(os.getenv("TILT_RATE_MIN", 1.8)),
        tilt_rate_max=float(os.getenv("TILT_RATE_MAX", 150.0)),
        focus_slope=float(os.getenv("FOCUS_SLOPE", 0.0006)),
        focus_intercept=float(os.getenv("FOCUS_INTERCEPT", 54)),
        focus_min=int(os.getenv("FOCUS_MIN", 5555)),
        focus_max=int(os.getenv("FOCUS_MAX", 9999)),
        jpeg_resolution=os.getenv("JPEG_RESOLUTION", "1920x1080"),
        jpeg_compression=int(os.getenv("JPEG_COMPRESSION", 5)),
        use_mqtt=ast.literal_eval(os.getenv("USE_MQTT", "True")),
        use_camera=ast.literal_eval(os.getenv("USE_CAMERA", "True")),
        include_age=ast.literal_eval(os.getenv("INCLUDE_AGE", "True")),
        log_to_mqtt=ast.literal_eval(os.getenv("LOG_TO_MQTT", "False")),
    )


if __name__ == "__main__":
    # Instantiate controller and execute
    controller = make_controller()
    controller.main()
