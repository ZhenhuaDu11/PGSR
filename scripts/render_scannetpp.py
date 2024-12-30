#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#
import os, sys
from pathlib import Path
dir_path = Path(os.path.dirname(os.path.realpath(__file__))).parents[0]
print(f"dir_path {dir_path}")
sys.path.append(dir_path.__str__())

import torch
from scene import Scene
import json
from tqdm import tqdm
from os import makedirs
from gaussian_renderer import render
import torchvision
from utils.general_utils import safe_state
from argparse import ArgumentParser
from arguments import ModelParams, PipelineParams, get_combined_args
from gaussian_renderer import GaussianModel
import numpy as np
import cv2
import open3d as o3d
from scene.app_model import AppModel
import trimesh, copy
from collections import deque
from utils.mesh_utils import post_process_mesh

import torchvision
import utils.vis_utils as VISUils


def render_set(model_path, name, iteration, views, scene, gaussians, pipeline, background, 
               app_model=None, max_depth=5.0, volume=None, use_depth_filter=False):
    # box
    js_file = f"{scene.source_path}/transforms.json"
    bounds = None
    if os.path.exists(js_file):
        with open(js_file) as file:
            meta = json.load(file)
            if "aabb_range" in meta:
                bounds = (np.array(meta["aabb_range"]))
    gts_path = os.path.join(model_path, name, "ours_{}".format(iteration), "gt")
    render_path = os.path.join(model_path, name, "ours_{}".format(iteration), "renders")
    render_depth_path = os.path.join(model_path, name, "ours_{}".format(iteration), "renders_depth")
    render_normal_path = os.path.join(model_path, name, "ours_{}".format(iteration), "renders_normal")

    render_mask_path = os.path.join(model_path, name, "ours_{}".format(iteration), "renders_mask")
    render_depth2plane_path = os.path.join(model_path, name, "ours_{}".format(iteration), "renders_depth2plane")
   
    makedirs(gts_path, exist_ok=True)
    makedirs(render_path, exist_ok=True)
    makedirs(render_depth_path, exist_ok=True)
    makedirs(render_normal_path, exist_ok=True)

    makedirs(render_mask_path, exist_ok=True)
    makedirs(render_depth2plane_path, exist_ok=True)

    depths_tsdf_fusion = []
    for idx, view in enumerate(tqdm(views, desc="Rendering progress")):
        gt, _ = view.get_image()
        out = render(view, gaussians, pipeline, background, app_model=app_model)
        rendering = out["render"].clamp(0.0, 1.0)
        _, H, W = rendering.shape

        # mask
        mask = out['rendered_alpha']
        torchvision.utils.save_image(mask, os.path.join(render_mask_path, view.image_name + ".jpg"))

        #depth
        depth = out["plane_depth"].squeeze()
        depth_tsdf = depth.clone()
        depth = depth.detach().cpu().numpy()
        # depth_i = (depth - depth.min()) / (depth.max() - depth.min() + 1e-20)
        # depth_i = (depth_i * 255).clip(0, 255).astype(np.uint8)
        # depth_color = cv2.applyColorMap(depth_i, cv2.COLORMAP_JET)
        np.savez(os.path.join(render_depth_path, view.image_name + ".npz"), depth)

        depth_color = VISUils.apply_depth_colormap(out["plane_depth"][0,...,None], mask[0,...,None]).detach()
        torchvision.utils.save_image(depth_color.permute(2,0,1), os.path.join(render_depth_path, view.image_name + ".jpg"))

        # plane depth
        depth2plane_color = VISUils.apply_depth_colormap(out["rendered_distance"][0,...,None], mask[0,...,None]).detach()
        torchvision.utils.save_image(depth2plane_color.permute(2,0,1), os.path.join(render_depth2plane_path, view.image_name + ".jpg"))
        
        # normal
        normal = out["rendered_normal"].permute(1,2,0)
        normal = normal/(normal.norm(dim=-1, keepdim=True)+1.0e-8)
        # normal = normal.detach().cpu().numpy()
        # normal = ((normal+1) * 127.5).astype(np.uint8).clip(0, 255)
        normal_w = (normal @ (view.world_view_transform[:3,:3].T)).permute(2,0,1)
        np.savez(os.path.join(render_normal_path, view.image_name + ".npz"), normal_w.permute(1,2,0).cpu().numpy())

        normal_w = ((normal_w+1)/2).clip(0, 1)
        torchvision.utils.save_image(normal_w, os.path.join(render_normal_path, view.image_name + ".jpg"))

        if name == 'test':
            torchvision.utils.save_image(gt.clamp(0.0, 1.0), os.path.join(gts_path, view.image_name + ".jpg"))
            torchvision.utils.save_image(rendering, os.path.join(render_path, view.image_name + ".jpg"))
        else:
            torchvision.utils.save_image(gt.clamp(0.0, 1.0), os.path.join(gts_path, view.image_name + ".jpg"))
            
            rendering_np = (rendering.permute(1,2,0).clamp(0,1)[:,:,[2,1,0]]*255).detach().cpu().numpy().astype(np.uint8)
            cv2.imwrite(os.path.join(render_path, view.image_name + ".jpg"), rendering_np)
        
        # cv2.imwrite(os.path.join(render_depth_path, view.image_name + ".jpg"), depth_color)
        # cv2.imwrite(os.path.join(render_normal_path, view.image_name + ".jpg"), normal)

        if use_depth_filter:
            view_dir = torch.nn.functional.normalize(view.get_rays(), p=2, dim=-1)
            depth_normal = out["depth_normal"].permute(1,2,0)
            depth_normal = torch.nn.functional.normalize(depth_normal, p=2, dim=-1)
            dot = torch.sum(view_dir*depth_normal, dim=-1)
            angle = torch.acos(dot)
            mask = angle > (100.0 / 180 * 3.14159)
            depth_tsdf[~mask] = 0
        depths_tsdf_fusion.append(depth_tsdf.squeeze())
        
    if volume is not None:
        depths_tsdf_fusion = torch.stack(depths_tsdf_fusion, dim=0)
        for idx, view in enumerate(tqdm(views, desc="TSDF Fusion progress")):
            ref_depth = depths_tsdf_fusion[idx]
            if use_depth_filter and len(view.nearest_id) > 2:
                nearest_world_view_transforms = scene.world_view_transforms[view.nearest_id]
                num_n = nearest_world_view_transforms.shape[0]
                ## compute geometry consistency mask
                H, W = ref_depth.squeeze().shape

                ix, iy = torch.meshgrid(
                    torch.arange(W), torch.arange(H), indexing='xy')
                pixels = torch.stack([ix, iy], dim=-1).float().to(out['plane_depth'].device)

                pts = gaussians.get_points_from_depth(view, ref_depth)
                pts_in_nearest_cam = torch.matmul(nearest_world_view_transforms[:,None,:3,:3].expand(num_n,H*W,3,3).transpose(-1,-2), 
                                                  pts[None,:,:,None].expand(num_n,H*W,3,1))[...,0] + nearest_world_view_transforms[:,None,3,:3] # b, pts, 3

                depths_nearest = depths_tsdf_fusion[view.nearest_id][:,None]
                pts_projections = torch.stack(
                                [pts_in_nearest_cam[...,0] * view.Fx / pts_in_nearest_cam[...,2] + view.Cx,
                                pts_in_nearest_cam[...,1] * view.Fy / pts_in_nearest_cam[...,2] + view.Cy], -1).float()
                d_mask = (pts_projections[..., 0] > 0) & (pts_projections[..., 0] < view.image_width) &\
                         (pts_projections[..., 1] > 0) & (pts_projections[..., 1] < view.image_height)

                pts_projections[..., 0] /= ((view.image_width - 1) / 2)
                pts_projections[..., 1] /= ((view.image_height - 1) / 2)
                pts_projections -= 1
                pts_projections = pts_projections.view(num_n, -1, 1, 2)
                map_z = torch.nn.functional.grid_sample(input=depths_nearest,
                                                        grid=pts_projections,
                                                        mode='bilinear',
                                                        padding_mode='border',
                                                        align_corners=True
                                                        )[:,0,:,0]
                
                pts_in_nearest_cam[...,0] = pts_in_nearest_cam[...,0]/pts_in_nearest_cam[...,2]*map_z.squeeze()
                pts_in_nearest_cam[...,1] = pts_in_nearest_cam[...,1]/pts_in_nearest_cam[...,2]*map_z.squeeze()
                pts_in_nearest_cam[...,2] = map_z.squeeze()
                pts_ = (pts_in_nearest_cam-nearest_world_view_transforms[:,None,3,:3])
                pts_ = torch.matmul(nearest_world_view_transforms[:,None,:3,:3].expand(num_n,H*W,3,3), 
                                    pts_[:,:,:,None].expand(num_n,H*W,3,1))[...,0]

                pts_in_view_cam = pts_ @ view.world_view_transform[:3,:3] + view.world_view_transform[None,None,3,:3]
                pts_projections = torch.stack(
                            [pts_in_view_cam[...,0] * view.Fx / pts_in_view_cam[...,2] + view.Cx,
                            pts_in_view_cam[...,1] * view.Fy / pts_in_view_cam[...,2] + view.Cy], -1).float()
                pixel_noise = torch.norm(pts_projections.reshape(num_n, H, W, 2) - pixels[None], dim=-1)
                d_mask_all = d_mask.reshape(num_n,H,W) & (pixel_noise < 1.0) & (pts_in_view_cam[...,2].reshape(num_n,H,W) > 0.1)
                d_mask_all = (d_mask_all.sum(0) > 1)
                ref_depth[~d_mask_all] = 0

            if bounds is not None:
                pts = gaussians.get_points_from_depth(view, ref_depth)
                unvalid_mask = (pts[...,0] < bounds[0,0]) | (pts[...,0] > bounds[0,1]) |\
                                (pts[...,1] < bounds[1,0]) | (pts[...,1] > bounds[1,1]) |\
                                (pts[...,2] < bounds[2,0]) | (pts[...,2] > bounds[2,1])
                unvalid_mask = unvalid_mask.reshape(H,W)
                ref_depth[unvalid_mask] = 0

            ref_depth = ref_depth.detach().cpu().numpy()
            
            pose = np.identity(4)
            pose[:3,:3] = view.R.transpose(-1,-2)
            pose[:3, 3] = view.T
            color = o3d.io.read_image(os.path.join(render_path, view.image_name + ".jpg"))
            depth = o3d.geometry.Image((ref_depth*1000).astype(np.uint16))
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                color, depth, depth_scale=1000.0, depth_trunc=max_depth, convert_rgb_to_intensity=False)
            volume.integrate(
                rgbd,
                o3d.camera.PinholeCameraIntrinsic(W, H, view.Fx, view.Fy, view.Cx, view.Cy),
                pose)

def render_sets(dataset : ModelParams, iteration : int, pipeline : PipelineParams, skip_train : bool, skip_test : bool,
                 max_depth : float, voxel_size : float, sdf_trunc : float, use_depth_filter : bool):
    with torch.no_grad():
        gaussians = GaussianModel(dataset.sh_degree)
        scene = Scene(dataset, gaussians, load_iteration=iteration, shuffle=False)
        # app_model = AppModel()
        # app_model.load_weights(scene.model_path)
        # app_model.eval()
        # app_model.cuda()

        bounds = None
        js_file = f"{scene.source_path}/transforms.json"
        if os.path.exists(js_file):
            with open(js_file) as file:
                meta = json.load(file)
                if "aabb_range" in meta:
                    bounds = (np.array(meta["aabb_range"]))

        bg_color = [1,1,1] if dataset.white_background else [0, 0, 0]
        background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
        if bounds is not None:
            max_dis = np.max(bounds[:,1]-bounds[:,0])
            voxel_size = max_dis / 2048.0
        print(f"TSDF voxel_size {voxel_size}")
        volume = o3d.pipelines.integration.ScalableTSDFVolume(
            voxel_length=voxel_size,
            sdf_trunc=sdf_trunc,
            color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)

        if not skip_train:
            render_set(dataset.model_path, "train", scene.loaded_iter, scene.getTrainCameras(), scene, gaussians, pipeline, background, 
                       max_depth=max_depth, volume=volume, use_depth_filter=use_depth_filter)
            print(f"extract_triangle_mesh")
            mesh = volume.extract_triangle_mesh()

            if use_depth_filter:
                path = os.path.join(dataset.model_path, "mesh")
            else:
                path = os.path.join(dataset.model_path, "mesh_wo_depth_filter")
            os.makedirs(path, exist_ok=True)
            
            o3d.io.write_triangle_mesh(os.path.join(path, "tsdf_fusion.ply"), mesh, 
                                       write_triangle_uvs=True, write_vertex_colors=True, write_vertex_normals=True)
            # post-processing code of 2dgs
            mesh_post = post_process_mesh(mesh, cluster_to_keep=1)
            o3d.io.write_triangle_mesh(os.path.join(path, "tsdf_fusion_womask_post.ply"), mesh_post, 
                                       write_triangle_uvs=True, write_vertex_colors=True, write_vertex_normals=True)
        if not skip_test:
            render_set(dataset.model_path, "test", scene.loaded_iter, scene.getTestCameras(), scene, gaussians, pipeline, background)

if __name__ == "__main__":
    torch.set_num_threads(8)
    # Set up command line argument parser
    parser = ArgumentParser(description="Testing script parameters")
    model = ModelParams(parser, sentinel=True)
    pipeline = PipelineParams(parser)
    parser.add_argument("--iteration", default=-1, type=int)
    parser.add_argument("--skip_train", action="store_true")
    parser.add_argument("--skip_test", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max_depth", default=20.0, type=float)
    parser.add_argument("--voxel_size", default=0.002, type=float)
    parser.add_argument("--sdf_trunc", default=0.008, type=float)
    parser.add_argument("--use_depth_filter", action="store_true")

    args = get_combined_args(parser)
    print("Rendering " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)
    print(f"multi_view_num {model.multi_view_num}")
    render_sets(model.extract(args), args.iteration, pipeline.extract(args), args.skip_train, args.skip_test, args.max_depth, args.voxel_size, args.sdf_trunc, args.use_depth_filter)