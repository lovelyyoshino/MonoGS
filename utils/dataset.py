import csv
import glob
import os
import shutil

import cv2
import numpy as np
import torch
import random
import trimesh
from PIL import Image

from gaussian_splatting.utils.graphics_utils import focal2fov

try:
    import pyrealsense2 as rs
except Exception:
    pass

from unidepth.models import UniDepthV2
import rclpy
from sensor_msgs.msg import Image as ROSImage, CameraInfo
from sensor_msgs.msg import PointCloud2
from sensor_msgs import point_cloud2
import pcl
from pcl import PointCloud_PointXYZRGB
from cv_bridge import CvBridge, CvBridgeError
import message_filters
import threading

class ReplicaParser:
    def __init__(self, input_folder):
        self.input_folder = input_folder
        self.color_paths = sorted(glob.glob(f"{self.input_folder}/results/frame*.jpg"))
        self.depth_paths = sorted(glob.glob(f"{self.input_folder}/results/depth*.png"))
        self.n_img = len(self.color_paths)
        self.load_poses(f"{self.input_folder}/traj.txt")

    def load_poses(self, path):
        self.poses = []
        with open(path, "r") as f:
            lines = f.readlines()

        frames = []
        for i in range(self.n_img):
            line = lines[i]
            pose = np.array(list(map(float, line.split()))).reshape(4, 4)
            pose = np.linalg.inv(pose)
            self.poses.append(pose)
            frame = {
                "file_path": self.color_paths[i],
                "depth_path": self.depth_paths[i],
                "transform_matrix": pose.tolist(),
            }

            frames.append(frame)
        self.frames = frames


class TUMParser:
    def __init__(self, input_folder):
        self.depth_model = UniDepthV2.from_pretrained("lpiccinelli/unidepth-v2-vitl14")
        self.depth_model.to("cuda:0")
        self.input_folder = input_folder
        self.intrinsics_list = []
        self.intrensics = None
        self.load_poses(self.input_folder, frame_rate=32)
        self.n_img = len(self.color_paths)

    def parse_list(self, filepath, skiprows=0):
        data = np.loadtxt(filepath, delimiter=" ", dtype=np.unicode_, skiprows=skiprows)
        return data

    def associate_frames(self, tstamp_image, tstamp_depth, tstamp_pose, max_dt=0.08):
        associations = []
        for i, t in enumerate(tstamp_image):
            if tstamp_pose is None:
                j = np.argmin(np.abs(tstamp_depth - t))
                if np.abs(tstamp_depth[j] - t) < max_dt:
                    associations.append((i, j))

            else:
                j = np.argmin(np.abs(tstamp_depth - t))
                k = np.argmin(np.abs(tstamp_pose - t))

                if (np.abs(tstamp_depth[j] - t) < max_dt) and (
                    np.abs(tstamp_pose[k] - t) < max_dt
                ):
                    associations.append((i, j, k))

        return associations
    
    def create_or_clean_directory(self, dir_path):
        # Check if the directory already exists
        if os.path.exists(dir_path):
            # If it exists, remove all files inside the directory
            for filename in os.listdir(dir_path):
                file_path = os.path.join(dir_path, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f'Failed to delete {file_path}. Reason: {e}')
        else:
            # If it does not exist, create the directory
            os.mkdir(dir_path)

    def load_poses(self, datapath, frame_rate=-1):
        if os.path.isfile(os.path.join(datapath, "groundtruth.txt")):
            pose_list = os.path.join(datapath, "groundtruth.txt")
        elif os.path.isfile(os.path.join(datapath, "pose.txt")):
            pose_list = os.path.join(datapath, "pose.txt")

        image_list = os.path.join(datapath, "rgb.txt")
        depth_list = os.path.join(datapath, "depth.txt")

        image_data = self.parse_list(image_list)
        depth_data = self.parse_list(depth_list)
        pose_data = self.parse_list(pose_list, skiprows=1)
        pose_vecs = pose_data[:, 0:].astype(np.float64)

        tstamp_image = image_data[:, 0].astype(np.float64)
        tstamp_depth = depth_data[:, 0].astype(np.float64)
        tstamp_pose = pose_data[:, 0].astype(np.float64)
        associations = self.associate_frames(tstamp_image, tstamp_depth, tstamp_pose)

        indicies = [0]
        for i in range(1, len(associations)):
            t0 = tstamp_image[associations[indicies[-1]][0]]
            t1 = tstamp_image[associations[i][0]]
            if t1 - t0 > 1.0 / frame_rate:
                indicies += [i]

        self.color_paths, self.poses, self.depth_paths, self.frames = [], [], [], []
        self.create_or_clean_directory(os.path.join(datapath, "neural_depth"))
        for ix in indicies:
            (i, j, k) = associations[ix]
            self.color_paths += [os.path.join(datapath, image_data[i, 1])]
            rgb = torch.from_numpy(np.array(Image.open(os.path.join(datapath, image_data[i, 1])))).permute(2, 0, 1)
            intrensics = torch.from_numpy(np.array([[535.4, 0.0, 539.2], [0.0, 320.1, 247.6], [0.0, 0.0, 1.0]]).astype(np.float32))
            predictions = self.depth_model.infer(rgb)#, intrensics)
            depth = predictions["depth"]
            intrinsics = predictions["K"].squeeze().cpu().numpy()  # Convert to 2D numpy array on the CPU
            self.intrinsics_list.append(intrinsics)
            depth = depth.squeeze().cpu().numpy()
            depth[np.isnan(depth)] = 0
            depth_pixels = (depth * 5000).astype(np.uint16)
            cv2.imwrite(os.path.join(datapath, "neural_depth", "depth_data_{}.png".format(ix)), depth_pixels)
            self.depth_paths += [os.path.join(datapath, "neural_depth", "depth_data_{}.png".format(ix))]

            quat = pose_vecs[k][4:]
            trans = pose_vecs[k][1:4]
            T = trimesh.transformations.quaternion_matrix(np.roll(quat, 1))
            T[:3, 3] = trans
            self.poses += [np.linalg.inv(T)]

            frame = {
                "file_path": str(os.path.join(datapath, image_data[i, 1])),
                "depth_path":  os.path.join(datapath, "neural_depth", "depth_data_{}.png".format(ix)), #"/home/hari/monoGS_ros_wrapper/UniDepth/dataset/depth_data_{}.png".format(ix),#str(os.path.join(datapath, depth_data[j, 1])),
                "transform_matrix": (np.linalg.inv(T)).tolist(),
            }
            self.frames.append(frame)
        self.depth_model.cpu()
        del self.depth_model
        self.intrensics = np.mean(self.intrinsics_list, axis=0)


class EuRoCParser:
    def __init__(self, input_folder, start_idx=0):
        self.input_folder = input_folder
        self.start_idx = start_idx
        self.color_paths = sorted(
            glob.glob(f"{self.input_folder}/mav0/cam0/data/*.png")
        )
        self.color_paths_r = sorted(
            glob.glob(f"{self.input_folder}/mav0/cam1/data/*.png")
        )
        assert len(self.color_paths) == len(self.color_paths_r)
        self.color_paths = self.color_paths[start_idx:]
        self.color_paths_r = self.color_paths_r[start_idx:]
        self.n_img = len(self.color_paths)
        self.load_poses(
            f"{self.input_folder}/mav0/state_groundtruth_estimate0/data.csv"
        )

    def associate(self, ts_pose):
        pose_indices = []
        for i in range(self.n_img):
            color_ts = float((self.color_paths[i].split("/")[-1]).split(".")[0])
            k = np.argmin(np.abs(ts_pose - color_ts))
            pose_indices.append(k)

        return pose_indices

    def load_poses(self, path):
        self.poses = []
        with open(path) as f:
            reader = csv.reader(f)
            header = next(reader)
            data = [list(map(float, row)) for row in reader]
        data = np.array(data)
        T_i_c0 = np.array(
            [
                [0.0148655429818, -0.999880929698, 0.00414029679422, -0.0216401454975],
                [0.999557249008, 0.0149672133247, 0.025715529948, -0.064676986768],
                [-0.0257744366974, 0.00375618835797, 0.999660727178, 0.00981073058949],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )

        pose_ts = data[:, 0]
        pose_indices = self.associate(pose_ts)

        frames = []
        for i in range(self.n_img):
            trans = data[pose_indices[i], 1:4]
            quat = data[pose_indices[i], 4:8]
            quat = quat[[1, 2, 3, 0]]
            
            
            T_w_i = trimesh.transformations.quaternion_matrix(np.roll(quat, 1))
            T_w_i[:3, 3] = trans
            T_w_c = np.dot(T_w_i, T_i_c0)

            self.poses += [np.linalg.inv(T_w_c)]

            frame = {
                "file_path": self.color_paths[i],
                "transform_matrix": (np.linalg.inv(T_w_c)).tolist(),
            }

            frames.append(frame)
        self.frames = frames


class BaseDataset(torch.utils.data.Dataset):
    def __init__(self, args, path, config):
        self.args = args
        self.path = path
        self.config = config
        self.device = "cuda:0"
        self.dtype = torch.float32
        self.num_imgs = 999999

    def __len__(self):
        return self.num_imgs

    def __getitem__(self, idx):
        pass


class MonocularDataset(BaseDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        calibration = config["Dataset"]["Calibration"]
        # Camera prameters
        self.fx = calibration["fx"]
        self.fy = calibration["fy"]
        self.cx = calibration["cx"]
        self.cy = calibration["cy"]
        self.width = calibration["width"]
        self.height = calibration["height"]
        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.K = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )
        # distortion parameters
        self.disorted = calibration["distorted"]
        self.dist_coeffs = np.array(
            [
                calibration["k1"],
                calibration["k2"],
                calibration["p1"],
                calibration["p2"],
                calibration["k3"],
            ]
        )
        self.map1x, self.map1y = cv2.initUndistortRectifyMap(
            self.K,
            self.dist_coeffs,
            np.eye(3),
            self.K,
            (self.width, self.height),
            cv2.CV_32FC1,
        )
        # depth parameters
        self.has_depth = True if "depth_scale" in calibration.keys() else False
        self.depth_scale = calibration["depth_scale"] if self.has_depth else None

        # Default scene scale
        nerf_normalization_radius = 5
        self.scene_info = {
            "nerf_normalization": {
                "radius": nerf_normalization_radius,
                "translation": np.zeros(3),
            },
        }

    def __getitem__(self, idx):
        color_path = self.color_paths[idx]
        pose = self.poses[idx]

        image = np.array(Image.open(color_path))
        depth = None

        if self.disorted:
            image = cv2.remap(image, self.map1x, self.map1y, cv2.INTER_LINEAR)

        if self.has_depth:
            depth_path = self.depth_paths[idx]
            depth = np.array(Image.open(depth_path)) / self.depth_scale

        image = (
            torch.from_numpy(image / 255.0)
            .clamp(0.0, 1.0)
            .permute(2, 0, 1)
            .to(device=self.device, dtype=self.dtype)
        )
        pose = torch.from_numpy(pose).to(device=self.device)
        return image, depth, pose


class StereoDataset(BaseDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        calibration = config["Dataset"]["Calibration"]
        self.width = calibration["width"]
        self.height = calibration["height"]

        cam0raw = calibration["cam0"]["raw"]
        cam0opt = calibration["cam0"]["opt"]
        cam1raw = calibration["cam1"]["raw"]
        cam1opt = calibration["cam1"]["opt"]
        # Camera prameters
        self.fx_raw = cam0raw["fx"]
        self.fy_raw = cam0raw["fy"]
        self.cx_raw = cam0raw["cx"]
        self.cy_raw = cam0raw["cy"]
        self.fx = cam0opt["fx"]
        self.fy = cam0opt["fy"]
        self.cx = cam0opt["cx"]
        self.cy = cam0opt["cy"]

        self.fx_raw_r = cam1raw["fx"]
        self.fy_raw_r = cam1raw["fy"]
        self.cx_raw_r = cam1raw["cx"]
        self.cy_raw_r = cam1raw["cy"]
        self.fx_r = cam1opt["fx"]
        self.fy_r = cam1opt["fy"]
        self.cx_r = cam1opt["cx"]
        self.cy_r = cam1opt["cy"]

        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.K_raw = np.array(
            [
                [self.fx_raw, 0.0, self.cx_raw],
                [0.0, self.fy_raw, self.cy_raw],
                [0.0, 0.0, 1.0],
            ]
        )

        self.K = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )

        self.Rmat = np.array(calibration["cam0"]["R"]["data"]).reshape(3, 3)
        self.K_raw_r = np.array(
            [
                [self.fx_raw_r, 0.0, self.cx_raw_r],
                [0.0, self.fy_raw_r, self.cy_raw_r],
                [0.0, 0.0, 1.0],
            ]
        )

        self.K_r = np.array(
            [[self.fx_r, 0.0, self.cx_r], [0.0, self.fy_r, self.cy_r], [0.0, 0.0, 1.0]]
        )
        self.Rmat_r = np.array(calibration["cam1"]["R"]["data"]).reshape(3, 3)

        # distortion parameters
        self.disorted = calibration["distorted"]
        self.dist_coeffs = np.array(
            [cam0raw["k1"], cam0raw["k2"], cam0raw["p1"], cam0raw["p2"], cam0raw["k3"]]
        )
        self.map1x, self.map1y = cv2.initUndistortRectifyMap(
            self.K_raw,
            self.dist_coeffs,
            self.Rmat,
            self.K,
            (self.width, self.height),
            cv2.CV_32FC1,
        )

        self.dist_coeffs_r = np.array(
            [cam1raw["k1"], cam1raw["k2"], cam1raw["p1"], cam1raw["p2"], cam1raw["k3"]]
        )
        self.map1x_r, self.map1y_r = cv2.initUndistortRectifyMap(
            self.K_raw_r,
            self.dist_coeffs_r,
            self.Rmat_r,
            self.K_r,
            (self.width, self.height),
            cv2.CV_32FC1,
        )

    def __getitem__(self, idx):
        color_path = self.color_paths[idx]
        color_path_r = self.color_paths_r[idx]

        pose = self.poses[idx]
        image = cv2.imread(color_path, 0)
        image_r = cv2.imread(color_path_r, 0)
        depth = None
        if self.disorted:
            image = cv2.remap(image, self.map1x, self.map1y, cv2.INTER_LINEAR)
            image_r = cv2.remap(image_r, self.map1x_r, self.map1y_r, cv2.INTER_LINEAR)
        stereo = cv2.StereoSGBM_create(minDisparity=0, numDisparities=64, blockSize=20)
        stereo.setUniquenessRatio(40)
        disparity = stereo.compute(image, image_r) / 16.0
        disparity[disparity == 0] = 1e10
        depth = 47.90639384423901 / (
            disparity
        )  ## Following ORB-SLAM2 config, baseline*fx
        depth[depth < 0] = 0
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        image = (
            torch.from_numpy(image / 255.0)
            .clamp(0.0, 1.0)
            .permute(2, 0, 1)
            .to(device=self.device, dtype=self.dtype)
        )
        pose = torch.from_numpy(pose).to(device=self.device)

        return image, depth, pose


class TUMDataset(MonocularDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        dataset_path = config["Dataset"]["dataset_path"]
        parser = TUMParser(dataset_path)
        self.num_imgs = parser.n_img
        self.color_paths = parser.color_paths
        self.depth_paths = parser.depth_paths
        self.poses = parser.poses
        self.fx = parser.intrensics[0, 0]
        self.fy = parser.intrensics[1, 1]
        self.cx = parser.intrensics[0, 2]
        self.cy = parser.intrensics[1, 2]
        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.K = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )


class ReplicaDataset(MonocularDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        dataset_path = config["Dataset"]["dataset_path"]
        parser = ReplicaParser(dataset_path)
        self.num_imgs = parser.n_img
        self.color_paths = parser.color_paths
        self.depth_paths = parser.depth_paths
        self.poses = parser.poses


class EurocDataset(StereoDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        dataset_path = config["Dataset"]["dataset_path"]
        parser = EuRoCParser(dataset_path, start_idx=config["Dataset"]["start_idx"])
        self.num_imgs = parser.n_img
        self.color_paths = parser.color_paths
        self.color_paths_r = parser.color_paths_r
        self.poses = parser.poses


class RealsenseDataset(BaseDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        self.pipeline = rs.pipeline()
        self.h, self.w = 720, 1280
        
        self.depth_scale = 0
        if self.config["Dataset"]["sensor_type"] == "depth":
            self.has_depth = True 
        else: 
            self.has_depth = False

        self.rs_config = rs.config()
        self.rs_config.enable_stream(rs.stream.color, self.w, self.h, rs.format.bgr8, 30)
        if self.has_depth:
            self.rs_config.enable_stream(rs.stream.depth)

        self.profile = self.pipeline.start(self.rs_config)

        if self.has_depth:
            self.align_to = rs.stream.color
            self.align = rs.align(self.align_to)

        self.rgb_sensor = self.profile.get_device().query_sensors()[1]
        self.rgb_sensor.set_option(rs.option.enable_auto_exposure, False)
        # rgb_sensor.set_option(rs.option.enable_auto_white_balance, True)
        self.rgb_sensor.set_option(rs.option.enable_auto_white_balance, False)
        self.rgb_sensor.set_option(rs.option.exposure, 200)
        self.rgb_profile = rs.video_stream_profile(
            self.profile.get_stream(rs.stream.color)
        )
        self.rgb_intrinsics = self.rgb_profile.get_intrinsics()
        
        self.fx = self.rgb_intrinsics.fx
        self.fy = self.rgb_intrinsics.fy
        self.cx = self.rgb_intrinsics.ppx
        self.cy = self.rgb_intrinsics.ppy
        self.width = self.rgb_intrinsics.width
        self.height = self.rgb_intrinsics.height
        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.K = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )

        self.disorted = True
        self.dist_coeffs = np.asarray(self.rgb_intrinsics.coeffs)
        self.map1x, self.map1y = cv2.initUndistortRectifyMap(
            self.K, self.dist_coeffs, np.eye(3), self.K, (self.w, self.h), cv2.CV_32FC1
        )

        if self.has_depth:
            self.depth_sensor = self.profile.get_device().first_depth_sensor()
            self.depth_scale  = self.depth_sensor.get_depth_scale()
            self.depth_profile = rs.video_stream_profile(
                self.profile.get_stream(rs.stream.depth)
            )
            self.depth_intrinsics = self.depth_profile.get_intrinsics()

    def __getitem__(self, idx):
        pose = torch.eye(4, device=self.device, dtype=self.dtype)
        depth = None

        frameset = self.pipeline.wait_for_frames()

        if self.has_depth:
            aligned_frames = self.align.process(frameset)
            rgb_frame = aligned_frames.get_color_frame()
            aligned_depth_frame = aligned_frames.get_depth_frame()
            depth = np.array(aligned_depth_frame.get_data())*self.depth_scale
            depth[depth < 0] = 0
            np.nan_to_num(depth, nan=1000)
        else:
            rgb_frame = frameset.get_color_frame()

        image = np.asanyarray(rgb_frame.get_data())
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        if self.disorted:
            image = cv2.remap(image, self.map1x, self.map1y, cv2.INTER_LINEAR)

        image = (
            torch.from_numpy(image / 255.0)
            .clamp(0.0, 1.0)
            .permute(2, 0, 1)
            .to(device=self.device, dtype=self.dtype)
        )

        return image, depth, pose

class ROSDataset(BaseDataset):
    def __init__(self, args, path, config):
        super().__init__(args, path, config)
        self.depth_model = None
        # if self.config["ROS_topics"]["depth_topic"] == 'None' or self.config["ROS_topics"]["camera_info_topic"] == 'None':
        #     self.depth_model = UniDepthV2.from_pretrained("lpiccinelli/unidepth-v2-vitl14")
        #     self.depth_model.to("cuda:0")
        self.bridge = CvBridge()
        self.image = None
        self.pointcloud = None
        self.image_received = False
        self.pointcloud_received = False
        self.depth = None
        self.fx = None
        self.fy = None
        self.cx = None
        self.cy = None
        self.K = None
        self.width = None
        self.height = None
        self.fovx = None
        self.fovy = None
        self.dist_coeffs = None
        self.node = rclpy.create_node("monoGS_dataloader")
        if self.config["ROS_topics"]["camera_info_topic"] != 'None':
            self.node.get_logger().info("Camera Info topic provided")
            self.disorted = None
            self.cameraInfo_sub = self.node.create_subscription(CameraInfo, str(self.config["ROS_topics"]["camera_info_topic"]), self.cameraInfo_callback, 1)
        else:
            self.node.get_logger().warn("Camera Info not provided, UniDepthV2 will estimate intrensics/parameters and assume image is not distorted!")
            self.disorted = False
        if self.config["ROS_topics"]["depth_topic"] == 'None' and self.config["ROS_topics"]["pointcloud_topic"] == 'None':
            self.image_sub = self.node.create_subscription(ROSImage, str(self.config["ROS_topics"]["camera_topic"]), self.image_callback, 1)
            self.node.get_logger().warn("Depth topic not provided, depth will be estimated by UniDepthV2!")
        elif self.config["ROS_topics"]["depth_topic"] != 'None':
            # Create subscribers with message filters
            self.image_sub = message_filters.Subscriber(self.node, ROSImage, str(self.config["ROS_topics"]["camera_topic"]))
            self.depth_sub = message_filters.Subscriber(self.node, ROSImage, self.config["ROS_topics"]["depth_topic"])
            # Synchronize the topics
            self.ts = message_filters.ApproximateTimeSynchronizer([self.image_sub, self.depth_sub], queue_size = 1, slop = 0.1)
            self.ts.registerCallback(self.common_callback)
        else:
            self.pointcloud_sub = self.node.create_subscription(self.node, PointCloud2, str(self.config["ROS_topics"]["pointcloud_topic"]), self.pointcloudxyzrgb_callback, 1)
            self.node.get_logger().warn("Get Pointcloud topic, this need by Livox Color!")


        # self.depth_scale = float(self.config["ROS_topics"]['depth_scale'])
        # self.has_depth = True if self.config["Dataset"]["sensor_type"] == "depth" else False
        
        # while self.__check_all_parameters__() or (not self.image_received and not self.pointcloud_received) :
        #     self.node.get_logger().warn("Waiting for camera to start and camera intrensics/parameters to get set....")
        #     rclpy.spin_once(self.node, timeout_sec=0.1)
        #     if self.config["ROS_topics"]["camera_info_topic"] == 'None' and self.image is not None:
        #         self.estimateIntrensics(self.image)
        # if not self.__check_all_parameters__() and self.config["ROS_topics"]["camera_info_topic"] != 'None':
        #     self.node.destroy_subscription(self.cameraInfo_sub)
        #     self.node.get_logger().info("Successfully loaded intrensics/camera parameters.")
        # self.map1x, self.map1y = None, None
        # if self.disorted:
        #     self.map1x, self.map1y = cv2.initUndistortRectifyMap(
        #         self.K, self.dist_coeffs, np.eye(3), self.K, (self.width, self.height), cv2.CV_32FC1
        #     )
        # Spin node in a separate thread
        self.spin_thread = threading.Thread(target=self.spin)
        self.spin_thread.start()
    
    def __check_all_parameters__(self):
        return (self.fx is None or
                self.fy is None or
                self.cx is None or
                self.cy is None or
                self.K is None or
                self.width is None or
                self.height is None or
                self.fovx is None or
                self.fovy is None or
                self.dist_coeffs is None)

    def cameraInfo_callback(self, msg):
        self.fx = msg.k[0]
        self.fy = msg.k[4]
        self.cx = msg.k[2]
        self.cy = msg.k[5]
        self.width = msg.width
        self.height = msg.height
        self.dist_coeffs = np.array(msg.d)
        self.disorted = False if np.all(np.isclose(self.dist_coeffs, 0)) else True
        self.K = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )
        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.node.get_logger().info("Camera parameters set.")

    def pointcloudxyzrgb_callback(self, msg):
        assert isinstance(msg, PointCloud2)
        self.pointcloud_received = True
        self.node.get_logger().info("Pointcloud received.")
        try:
            gen = point_cloud2.read_points(msg, field_names=("x", "y", "z", "rgb"), skip_nans=True)
             # 将点云数据转换为PCL格式
            pcl_cloud = pcl.PointCloud_PointXYZRGB()
            for p in gen:
                pcl_cloud.push_back([p[0], p[1], p[2], p[3]])
            self.pointcloud = pcl_cloud
             # 处理点云数据，生成图像和深度图
            self.rgb, self.depth = self.process_pointcloud(pcl_cloud)
        except Exception as e:
            self.node.get_logger().error("Error: {}".format(e))

    def process_pointcloud(self, pcl_cloud):
        # 获取点云中的点数
        points = np.asarray(pcl_cloud)
        
        # 初始化图像和深度图
        width, height = 640, 480
        rgb_image = np.zeros((height, width, 3), dtype=np.uint8)
        depth_image = np.zeros((height, width), dtype=np.float32)
        
        # 投影点云到2D图像平面
        for point in points:
            x, y, z, rgb = point
            # 这里假设一个简单的正交投影，可以根据需要修改为透视投影
            u = int((x + 10) * width / 20)
            v = int((y + 10) * height / 20)
            if 0 <= u < width and 0 <= v < height:
                rgb_image[v, u] = self.unpack_rgb(rgb)
                depth_image[v, u] = z

        return rgb_image, depth_image

    def unpack_rgb(self, rgb):
        # 将单个浮点数RGB值转换为三个8位整数
        rgb = int(rgb)
        r = (rgb >> 16) & 0x0000ff
        g = (rgb >> 8) & 0x0000ff
        b = (rgb) & 0x0000ff
        return [r, g, b]


    def image_callback(self, msg):
        self.image_received = True
        try:
            self.image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except CvBridgeError as e:
            self.node.get_logger().error("Error: {}".format(e))
    
    def common_callback(self, image_msg, depth_msg):
        self.image_received = True
        try:
            self.image = self.bridge.imgmsg_to_cv2(image_msg, desired_encoding='bgr8')
            self.depth = self.bridge.imgmsg_to_cv2(depth_msg, desired_encoding='passthrough')
        except CvBridgeError as e:
            self.node.get_logger().error("Error: {}".format(e))

    def spin(self):
        while rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0.1)

    def __getitem__(self, idx):
        pose = torch.eye(4, device=self.device, dtype=self.dtype)
        image = cv2.cvtColor(self.image, cv2.COLOR_BGR2RGB)
        if self.config["ROS_topics"]["depth_topic"] == 'None':
            depth = self.generateDepth(image)
        else:
            depth = self.depth
        if self.disorted:
            image = cv2.remap(image, self.map1x, self.map1y, cv2.INTER_LINEAR)
        image = (
            torch.from_numpy(image / 255.0)
            .clamp(0.0, 1.0)
            .permute(2, 0, 1)
            .to(device=self.device, dtype=self.dtype)
        )

        return image, depth, pose
    
    def generateDepth(self, rgb):
        rgb_image = torch.from_numpy(rgb).permute(2, 0, 1)
        intrensics = torch.from_numpy(self.K.astype(np.float32))
        if self.config["ROS_topics"]["camera_info_topic"] != 'None':
            predictions = self.depth_model.infer(rgb_image, intrensics)
        else:
            predictions = self.depth_model.infer(rgb_image)
        depth = predictions["depth"]
        depth = depth.squeeze().cpu().numpy()
        depth[np.isnan(depth)] = 0
        return depth
    
    def estimateIntrensics(self, bgr):
        rgb_image = torch.from_numpy(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).permute(2, 0, 1)
        predictions = self.depth_model.infer(rgb_image)
        self.K = predictions["K"].squeeze().cpu().numpy()
        self.fx = self.K[0, 0]
        self.fy = self.K[1, 1]
        self.cx = self.K[0, 2]
        self.cy = self.K[1, 2]
        self.height, self.width, _ = bgr.shape
        self.fovx = focal2fov(self.fx, self.width)
        self.fovy = focal2fov(self.fy, self.height)
        self.dist_coeffs = np.zeros(8)
        self.node.get_logger().warn("Camera Intrensics/parameters set and estimated by UniDepthV2")

def load_dataset(args, path, config):
    if config["Dataset"]["type"] == "tum":
        return TUMDataset(args, path, config)
    elif config["Dataset"]["type"] == "replica":
        return ReplicaDataset(args, path, config)
    elif config["Dataset"]["type"] == "euroc":
        return EurocDataset(args, path, config)
    elif config["Dataset"]["type"] == "realsense":
        return RealsenseDataset(args, path, config)
    elif config["Dataset"]["type"] == "ROS":
        return ROSDataset(args, path, config)
    else:
        raise ValueError("Unknown dataset type")
