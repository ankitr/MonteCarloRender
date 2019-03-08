#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jan 30 13:00:00 2019

@author: guysatat
"""

import numpy as np
from multiprocessing import Pool
from itertools import repeat
from numba import cuda
from numba.cuda.random import create_xoroshiro128p_states, xoroshiro128p_uniform_float32
import sys
import math
import threading

#Assumptions:
# 1. Detector is circular, at 0,0,0 on the x-y plane and points towards the positive z axis
# 2. Doesn't dupport reflective targets.
# 3. Support only impulse pencil beam.

#compiler_lock = threading.Lock()

# Note: batchSize         = max number of photons per device (affected by memory considerations)
#       nPhotonsRequested = the size of the array asked to be returned - how many photons should be detected
#       nPhotonsToRun     = the maximum photons to run before giving up
def lunchPacketwithBatch(batchSize = 1000,
                        nPhotonsRequested = 1e6,
                        nPhotonsToRun = 1e10,
                        muS = 1.0, g = 0.85,
                        source = {'r': np.array([0.0, 0.0, 0.0]),
                                  'mu': np.array([0.0, 0.0, 1.0]),
                                  'method': 'pencil', 'time_profile': 'delta'},
                        detector = {'type': 0, 'radius': 10.0, 'z_detector': 10.0, 'det_size': 8.0},
                        control_param = {'max_N': 1e5,
                                         'max_distance_from_det': 1000},
                        normalize_d = None,
                        ret_cols = [0,1,2,3],
                        target = {'type':0,
                                  'mask':np.zeros(shape=(60,60)),
                                  'grid_size':np.array([1,1]),
                                  'z_target':20},
                        z_bounded = False,
                        z_range = np.array([0.0,30.0]),
                        device_id = 0
                        ):
    muS = float(muS)
    g = float(g)
    detR = float(detector['radius'])
    max_N = control_param['max_N']
    max_distance_from_det = float(control_param['max_distance_from_det'])
    
    detector_params = getDetectorParams(detector, target)
    detector_params = np.array(detector_params).astype(float)
    
    source_type, source_param1, source_param2 = simSource(source = source)
        
    target_type = target['type']
    target_mask = target['mask']
    target_gridsize = target['grid_size'].astype(float)
    z_target = target['z_target']
    
    z_range = z_range.astype(float)     
    
    nPhotonsRequested = int(nPhotonsRequested)
    batchSize = int(batchSize)
    data = np.ndarray(shape=(nPhotonsRequested, len(ret_cols)), dtype=float)
    
    photon_counters = np.zeros(shape=5, dtype=int)
# photon_counters description:
#    0: Total simulated photons
#    1: Detected photons
#    2: Photons that hit the target and then detected
#    3: Photons that hit the target
#    4: Photons that didn't get back to the detector because of some of the stopping criteria    
        
    while photon_counters[0] < nPhotonsToRun and photon_counters[1] < nPhotonsRequested:
        if device_id == -1: # Run multi GPU version
            ret = MultiGPUWrapper(batchSize, nPhotonsToRun, muS, g, 
                             source_type, source_param1, source_param2, 
                             detector_params, 
                             max_N, max_distance_from_det, 
                             target_type, target_mask, target_gridsize, 
                             z_target, z_bounded, z_range, ret_cols)
        else: # Run on specific GPU (device_id)
            ret = SingleGPUWrapper(device_id, batchSize, nPhotonsToRun,
                                   muS, g, 
                                   source_type, source_param1, source_param2, 
                                   detector_params, 
                                   max_N, max_distance_from_det, 
                                   target_type, target_mask, target_gridsize, 
                                   z_target, z_bounded, z_range, ret_cols)
        ret_data = ret[0]
        # Not valid photons return with n=-1 - so remove them
        ret_data = ret_data[ret_data[:, 0]>0, :]
        
        if ret_data.shape[0] > nPhotonsRequested - photon_counters[1]:
            ret_data = ret_data[:nPhotonsRequested - photon_counters[1], :]
        data[photon_counters[1] : (photon_counters[1]+ret_data.shape[0]), :] = ret_data

        photon_counters += ret[1]
            
    data = data[:photon_counters[1], :] 
    if normalize_d is not None:
        data[:, 1] *= normalize_d
    return data, photon_counters


def SingleGPUWrapper(device_id, photons_requested, max_photons_to_run,
                     muS, g, 
                     source_type, source_param1, source_param2, 
                     detector_params, 
                     max_N, max_distance_from_det, 
                     target_type, target_mask, target_gridsize, 
                     z_target, z_bounded, z_range, ret_cols):
    data_out = {}
    data_out[device_id] = [0,0]
    
    photons_req_per_device = photons_requested
    max_photons_per_device = max_photons_to_run
    
    GPUWrapper(data_out, device_id,
               photons_req_per_device, max_photons_per_device,
               muS, g, 
               source_type, source_param1, source_param2, 
               detector_params, 
               max_N, max_distance_from_det, 
               target_type, target_mask, target_gridsize, 
               z_target, z_bounded, z_range, ret_cols)
    return data_out[device_id]


def MultiGPUWrapper(photons_requested, max_photons_to_run,
                    muS, g, 
                    source_type, source_param1, source_param2, 
                    detector_params, 
                    max_N, max_distance_from_det, 
                    target_type, target_mask, target_gridsize, 
                    z_target, z_bounded, z_range, ret_cols):
    data_out = {}
    children = []
    n_GPUs = len(cuda.list_devices())
    
    # I've decided to use the photon numbers as is - it means that the requests should be treated
    # as a per GPU basis. The calling function is reponsible for dividing if neccesary.
    # photons_req_per_device = np.floor(photons_requested / n_GPUs)
    # max_photons_per_device = np.floor(max_photons_to_run / n_GPUs)
    
    photons_req_per_device = photons_requested
    max_photons_per_device = max_photons_to_run    
    
    for device_id, dev in enumerate(cuda.list_devices()):
        data_out[device_id] = [0,0]
        t = threading.Thread(target=GPUWrapper, args=(data_out, device_id, 
                                                      photons_req_per_device, max_photons_per_device, 
                                                      muS, g, 
                                                      source_type, source_param1, source_param2, 
                                                      detector_params, 
                                                      max_N, max_distance_from_det, 
                                                      target_type, target_mask, target_gridsize, 
                                                      z_target, z_bounded, z_range, ret_cols))
        t.start()
        children.append(t)

    for t in children:
        t.join()
    
    data_ret = None
    for i in range(n_GPUs):
        if data_ret is None:
            data_ret = data_out[i][0]
            photon_counters = data_out[i][1]
        else:
            data_ret = np.concatenate((data_ret, data_out[i][0]), axis=0)       
            photon_counters += data_out[i][1]
    
    return [data_ret, photon_counters]
    

def GPUWrapper(data_out, device_id, 
               photons_req_per_device, max_photons_per_device,
               muS, g, 
               source_type, source_param1, source_param2, 
               detector_params, 
               max_N, max_distance_from_det, 
               target_type, target_mask, target_gridsize, 
               z_target, z_bounded, z_range, ret_cols):
    
    threads_per_block = 256 
    blocks = 64
    photons_per_thread = int(np.ceil(float(photons_req_per_device)/(threads_per_block * blocks)))
    max_photons_per_thread = int(np.ceil(float(max_photons_per_device)/(threads_per_block * blocks)))
    
    cuda.select_device(device_id)
    device = cuda.get_current_device()
    stream = cuda.stream()  # use stream to trigger async memory transfer
    
    # Keeping this piece of code here for now -potentially we need this in the future
  #  with compiler_lock:                        # lock the compiler
        # prepare function for this thread
        # the jitted CUDA kernel is loaded into the current context
        # TODO: ideally we should call cuda.jit(signature)(propPhotonGPU), where
        # signature is the call to the function. So far I couldn't figure out what is the signature of the 
        # rng_states, closest I got to was: array(Record([('s0', '<u8'), ('s1', '<u8')]), 1d, A)
        # But I couldn't get it to work yet.
   #     MC_cuda_kernel = cuda.jit(propPhotonGPU)
        
    
    data = np.ndarray(shape=(threads_per_block*blocks, photons_per_thread, 11), dtype=np.float32)
    photon_counters = np.ndarray(shape=(threads_per_block*blocks, 5), dtype=np.int)
    data_out_device = cuda.device_array_like(data, stream=stream)
    photon_counters_device = cuda.device_array_like(photon_counters, stream=stream)
    
    rng_states = create_xoroshiro128p_states(threads_per_block * blocks, seed=(np.random.randint(sys.maxsize)-128)+device_id, stream=stream)
    
    propPhotonGPU[blocks, threads_per_block](rng_states, data_out_device, photon_counters_device,
                                             photons_per_thread, max_photons_per_thread, muS, g, 
                                             source_type, source_param1, source_param2, 
                                             detector_params, 
                                             max_N, max_distance_from_det, 
                                             target_type, target_mask, target_gridsize, 
                                             z_target, z_bounded, z_range)
    
    data_out_device.copy_to_host(data, stream=stream)
    photon_counters_device.copy_to_host(photon_counters, stream=stream)
    stream.synchronize()

    data = data.reshape(data.shape[0]*data.shape[1], data.shape[2])
    data = data[:, ret_cols]
    data_out[device_id][0] = data
    
    photon_counters_aggr = np.squeeze(np.sum(photon_counters, axis=0))
    data_out[device_id][1] = photon_counters_aggr
    




# Input
# =====
#  photons_requested: total photons in the detector (max returned in the array) 
#  max_photons_to_run: maximum of the photons that are allowed to simulate
#
#  target_type:
#         0: not simulated
#         1: absorbing target
#         2: scattering target
#
#  target_mask:
#         if target_type==2 (scattering target):
#            target_type[i,j]==0 : transparent
#            target_type[i,j]==1 : diffuse reflection (Lambertian)
#            target_type[i,j]==2 : mirror reflection (reflect along the z axis)
# 
# source_type:
#         0: pencile
#         1: cone
#         2: area
#         3: area+cone
#         4: structured pattern
#         5: structured pattern + cone
#
# source_param1: 
#    0, 1, 2,  3,   4,   5,      6,          7,     8, 9
#    x, y, z, nux, nuy, nuz, half_angle, area_size, d, n
#   cols 6,7 are used only if required by the chosen source_type
#   when soruce_type is structured pattern, 0, 1 are x_dim, y_dim of structured light (center of 2D pattern at (0,0))
#  
# detector_params:
#      0,        1,            2,          3,         4,           5,            6,              7 
#    Type, Aperture size, focal_length, thickness,  Radius, refraction index, z_detector, Detector size(side of square)-only for lens
#
#    detector types: 0: bare sensor (only Aperture size is used),  
#                    1: lens
#
#  Return columns:
#    0, 1, 2, 3, 4,   5,    6,    7,       8,            9,           10
#    n, d, x ,y, z, mu_x, mu_y, mu_z, n_hit_target, d_hit_target,  n_target_hit_times
#   cols 8,9 are updated only with scattering target
#
#   photon_counters:
#    0: Total simulated photons
#    1: Detected photons
#    2: Photons that hit the target and then detected
#    3: Photons that hit the target
#    4: Photons that didn't get back to the detector because of some of the stopping criteria

@cuda.jit
def propPhotonGPU(rng_states, data_out, photon_counters, 
                  photons_requested, max_photons_to_run, muS, g, 
                  source_type, source_param1, source_param2, 
                  detector_params, 
                  max_N, max_distance_from_det, 
                  target_type, target_mask, target_gridsize, z_target, 
                  z_bounded, z_range):
    
    thread_id = cuda.grid(1)
    target_x_dim = target_mask.shape[1]
    target_y_dim = target_mask.shape[0]
    x_center_index = target_x_dim / 2
    y_center_index = target_y_dim / 2
    z_min, z_max = z_range[0], z_range[1]
    detR2 = detector_params[1]**2
    
    if source_type in [4,5]:
        SL_x_center_index = source_param1[0]/2
        SL_y_center_index = source_param1[1]/2
        SL_list_length = len(source_param2)
        
    
    if source_type in [1,3,5]: #if random angle
        rand_mu = xoroshiro128p_uniform_float32(rng_states, thread_id)
        rand_psi = xoroshiro128p_uniform_float32(rng_states, thread_id)
    if source_type in [2,3,4,5]: #if random position
        rand_x = xoroshiro128p_uniform_float32(rng_states, thread_id)
        rand_y = xoroshiro128p_uniform_float32(rng_states, thread_id)
    if source_type in [4,5]: #if structured pattern
        rand_index = xoroshiro128p_uniform_float32(rng_states, thread_id)
    
    
    photon_cnt_tot = 0          # Total simulated photons
    photons_cnt_stopped = 0     # Photons that didn't get back to the detector because of some of the stopping criteria
    photons_cnt_hit_target = 0  # Photons that hit the target
    photons_cnt_detected = 0    # Detected photons
    photons_cnt_detected_hit_target = 0  # Photons that hit the target and then detected
    
    while photon_cnt_tot < max_photons_to_run and photons_cnt_detected < photons_requested:
        photon_cnt_tot += 1
        hit_target_flag = False
        
        data_out[thread_id, photons_cnt_detected, :] = -1.0
        data_out[thread_id, photons_cnt_detected, 10] = 0

        # Initialize photon based on the illumination type
        if source_type == 0 or source_type ==1 : # Fixed x,y,z (pencil, cone)
            x, y, z =  source_param1[0], source_param1[1], source_param1[2]
        elif source_type == 2 or source_type == 3: # Area source or area_cone_source 
            x = source_param1[0] + source_param1[7] * (rand_x - 0.5) #sample position 
            y = source_param1[1] + source_param1[7] * (rand_y - 0.5)
            z = source_param1[2]
        elif source_type == 4 or source_type == 5: # Structured Pattern
            x = source_param1[7] * (rand_x - 0.5) #sample position within a pixel
            y = source_param1[7] * (rand_y - 0.5)
            z = source_param1[2]
            random_index = math.floor(rand_index * SL_list_length) #sample pixel
            pattern_index = source_param2[int(random_index)]
            x_offset = ((pattern_index % source_param1[0]) - SL_x_center_index + 0.5) * source_param1[7]
            y_offset = (math.floor(pattern_index / source_param1[0]) - SL_y_center_index + 0.5) * source_param1[7]
            x += x_offset
            y += y_offset
        #get nux, nuy, nuz
        if source_type == 0 or source_type == 2 or source_type == 4: # Fixed angle (pencil, area)
            nux, nuy, nuz = source_param1[3], source_param1[4], source_param1[5]
        else: # Random angle with hald angle theta (cone or area_cone)
            mu = 1 - (1-math.cos(source_param1[6]))*rand_mu # Sample uniformaly between [cos(half_angle),1]
            psi = 2*math.pi*rand_psi
            sqrt_mu = math.sqrt(1-mu**2)
            sin_psi = math.sin(psi)
            cos_psi = math.cos(psi)
            sqrt_w  = math.sqrt(1-source_param1[5]**2)
            if source_param1[5] == 1.0:
                nux = sqrt_mu*cos_psi
                nuy = sqrt_mu*sin_psi
                nuz = mu 
            elif source_param1[5] == -1.0:
                nux = sqrt_mu*cos_psi
                nuy = -sqrt_mu*sin_psi
                nuz = -mu
            elif sqrt_w != 0:
                nux = source_param1[3]*mu +(source_param1[3]*source_param1[5]*cos_psi - source_param1[4]*sin_psi)*sqrt_mu/sqrt_w
                nuy = source_param1[4]*mu +(source_param1[4]*source_param1[5]*cos_psi + source_param1[3]*sin_psi)*sqrt_mu/sqrt_w
                nuz = source_param1[5]*mu - cos_psi*sqrt_mu*sqrt_w
        d, n = source_param1[8],source_param1[9]

        # Start Monte Carlo inifinite loop
        while True:
            # Should we stop
            if n >= max_N:
                photons_cnt_stopped += 1
                break
            if math.sqrt(x*x + y*y + z*z) > max_distance_from_det:  # Assumes detector at origin
                photons_cnt_stopped += 1
                break    
            if z_bounded:# Check if we are out of tissue (when starting from tissue z boundary)
                if z > z_max or z < z_min:
                    photons_cnt_stopped += 1
                    break
                    
            # Get random numbers    
            rand1 = xoroshiro128p_uniform_float32(rng_states, thread_id) 
            rand2 = xoroshiro128p_uniform_float32(rng_states, thread_id) 
            rand3 = xoroshiro128p_uniform_float32(rng_states, thread_id)             
            
            # Calc random prop distance
            cd = - math.log(rand1) / muS

            # Update temporary new location
            t_rx = x + cd * nux
            t_ry = y + cd * nuy
            t_rz = z + cd * nuz

            # Check if we hit the detector
            if t_rz <= 0: #did we pass the detector?
                cd = - z / nuz
    
                t_rx = x + cd * nux
                t_ry = y + cd * nuy
                t_rz = z + cd * nuz
            
                if t_rx**2 + t_ry**2 < detR2: # If we hit the aperture
                    # Photon was detected
                    d+=cd
                    n+=1
                    x,y,z = t_rx, t_ry, t_rz
                    
                    if detector_params[0]==1: # This is a lens
                        # Currently we have x,y,z,nux,nuy,nuz right before the lens                        
                        # Update angle and distance based on lens
                        # Propogation distance inside lens is: th - R + sqrt(R^2 - x^2) where th is thickness and R is radius
                        alphax = -math.atan(nux/nuz)
                        alphay = -math.atan(nuy/nuz)
                        alphax = alphax - x/detector_params[2]
                        alphay = alphay - y/detector_params[2]
                        d += (detector_params[3] - detector_params[4] + math.sqrt(detector_params[4]**2 - (x**2+y**2))) / detector_params[5]
                        
                        #Propogate to sensor
                        x += alphax * detector_params[6]
                        y += alphay * detector_params[6]
                        z -= detector_params[6]
                        d += math.sqrt( (alphax * detector_params[6])**2 + (alphay * detector_params[6])**2 + (detector_params[6])**2 )
                        nux, nuy, nuz = 0, 0, 0  # We don't bother recalculating these angles
                        
                        # make sure we hit the detector itself, otherwise stop
                        if (x < -detector_params[7]) or (x > detector_params[7]) or (y < -detector_params[7]) or (y > detector_params[7]):
                            photons_cnt_stopped += 1
                            break
                        
                    data_out[thread_id, photons_cnt_detected, 0] = n
                    data_out[thread_id, photons_cnt_detected, 1] = d
                    data_out[thread_id, photons_cnt_detected, 2] = x
                    data_out[thread_id, photons_cnt_detected, 3] = y
                    data_out[thread_id, photons_cnt_detected, 4] = z
                    data_out[thread_id, photons_cnt_detected, 5] = nux
                    data_out[thread_id, photons_cnt_detected, 6] = nuy
                    data_out[thread_id, photons_cnt_detected, 7] = nuz
                    photons_cnt_detected += 1
                    if hit_target_flag:
                        photons_cnt_detected_hit_target += 1
                    break
                else:  # If we passed the detector and didn't hit it we should stop
                    photons_cnt_stopped += 1
                    break
            
            if target_type > 0: # If target is simulated
                if (t_rz - z_target) * (z - z_target) <=0 : # If we passed the target plane. See if we hit target
                    # Update the cooridnates for hitting the target
                    cd_target = (z_target - z) / nuz
                    t_rx_target = x + cd_target * nux 
                    t_ry_target = y + cd_target * nuy
                    t_rz_target = z + cd_target * nuz
                    
                    x_index = int(math.floor(t_rx_target / target_gridsize[0] + x_center_index)) #center of camera at x=0
                    y_index = int(math.floor(t_ry_target / target_gridsize[1] + y_center_index)) #canter of cameta at y=0
                    
                    if target_type == 1: # If this is an absorbing target
                        if x_index < 0 or x_index >= target_x_dim or y_index < 0 or y_index >= target_y_dim:
                            photons_cnt_stopped += 1 #photon is out of the bound of target
                            break
                        elif target_mask[y_index,x_index] == 0:
                            photons_cnt_stopped += 1 #we are absorbed by target
                            break
                            
                    elif target_type == 2: # If this is a scattering target
                        if x_index >= 0 and x_index < target_x_dim and y_index >= 0 and y_index < target_y_dim:
                            if target_mask[y_index, x_index] > 0:  # 0 is transparent
                                if z > z_target: # We want to drop photons that hit the target on the backside
                                    photons_cnt_stopped += 1 # Hit target on back side
                                    break
                                    
                                # Update photon to hit the target
                                d += cd_target
                                n += 1
                                x, y, z = t_rx_target, t_ry_target, t_rz_target
                                z-=0.0001 # We want to shift the photon a little bit from the target, otherwise in the next loop it'll hit the target again by definition (z==z_target)
                                data_out[thread_id, photons_cnt_detected, 8] = n
                                data_out[thread_id, photons_cnt_detected, 9] = d
                                data_out[thread_id, photons_cnt_detected, 10] +=1
                                
                                if not hit_target_flag:  # Count photons that hit target only once
                                    photons_cnt_hit_target += 1
                                hit_target_flag = True
                                
                                
                                # Calculate scattering angle
                                if target_mask[y_index, x_index] == 1:  # 1 is lambertian reflection
                                    psi = 2 * math.pi * rand2
                                    mu = -rand3  # This is instead of (mu = 1 - 2 * rand3) because we know we want nuz to be negative (nuz = - abs(mu))
                                    sin_psi = math.sin(psi)
                                    cos_psi = math.cos(psi)
                                    sqrt_mu = math.sqrt(1-mu**2)                                
                                    nux = sqrt_mu*cos_psi
                                    nuy = -sqrt_mu*sin_psi
                                    nuz = mu
                                elif target_mask[y_index, x_index] == 2:  # 2 is mirror reflection
                                    nuz = -nuz

                                continue # Skip the "regular" photon update below
                                
                                

            # Update photon
            x, y, z = t_rx, t_ry, t_rz 
            d += cd #prop distance
            n += 1 #increase scatter counter

            # Scatter to new angle
            psi = 2 * math.pi * rand2
            mu = 1/(2*g) * (1+g**2 - ( (1-g*g)/(1-g+2*g*rand3))**2)

            # Update angels
            sin_psi = math.sin(psi)
            cos_psi = math.cos(psi)
            sqrt_mu = math.sqrt(1-mu**2)
            sqrt_w  = math.sqrt(1-nuz**2)
            if sqrt_w != 0:
                prev_nux, prev_nuy, prev_nuz = nux, nuy, nuz
                nux = prev_nux*mu + (prev_nux*prev_nuz*cos_psi - prev_nuy*sin_psi)*sqrt_mu/sqrt_w
                nuy = prev_nuy*mu + (prev_nuy*prev_nuz*cos_psi + prev_nux*sin_psi)*sqrt_mu/sqrt_w
                nuz = prev_nuz*mu - cos_psi*sqrt_mu*sqrt_w
            elif nuz==1.0:
                nux = sqrt_mu*cos_psi
                nuy = sqrt_mu*sin_psi
                nuz = mu
            else: # nu[2]==-1.0
                nux = sqrt_mu*cos_psi
                nuy = -sqrt_mu*sin_psi
                nuz = -mu
                
    # Update photon counters before completion
    photon_counters[thread_id, 0] = photon_cnt_tot  
    photon_counters[thread_id, 1] = photons_cnt_detected                
    photon_counters[thread_id, 2] = photons_cnt_detected_hit_target                
    photon_counters[thread_id, 3] = photons_cnt_hit_target  
    photon_counters[thread_id, 4] = photons_cnt_stopped  

    

                    
# returns source_type id, source_param1([x, y, z, nux, nuy, nuz, theta, grid_size (for area source), d, n]), 
#and source_param2(for 2D structured light).
def simSource(source = {'r': np.array([0.0, 0.0, 0.0]),
                       'mu': np.array([0.0, 0.0, 1.0]),
                       'method': 'pencil', 
                       'time_profile': 'delta'}):
    r0 = source['r']
    nu0 = source['mu']
    if source['method'] == 'pencil':
        source_type = 0
        #[r[0:2], nu[0:2],theta, grid_size (for area source) d, n]
        source_param1 = np.array([r0[0], r0[1], r0[2], nu0[0], nu0[1], nu0[2], 0.0, 0.0, 0]).astype(float)
        source_param2 = np.array([0]).astype(float)
    elif source['method'] == 'cone':
        source_type = 1
        theta = source['theta']
        #[r[0:2], nu[0:2],theta d, n]
        source_param1 = np.array([r0[0], r0[1], r0[2], nu0[0], nu0[1], nu0[2], theta,0.0, 0.0, 0]).astype(float)
        source_param2 = np.array([0]).astype(float)
    elif source['method'] == 'point': #point source is a special case of light cone
        source_type = 1
        theta = math.pi
        source_param1 = np.array([r0[0], r0[1], r0[2], 0, 0, -1, theta, 0.0, 0.0, 0]).astype(float)
        source_param2 = np.array([0]).astype(float)
    elif source['method'] == 'area': #area source with specified nu
        source_type = 2
        size = source['size']
        source_param1 = np.array([r0[0], r0[1], r0[2], nu0[0], nu0[1], nu0[2], 0.0, size, 0.0, 0]).astype(float)
        source_param2 = np.array([0]).astype(float)
    elif source['method'] == 'area_cone': #area source with specified nu
        source_type = 3
        size = source['size']
        theta = source['theta']
        source_param1 = np.array([r0[0], r0[1], r0[2], nu0[0], nu0[1], nu0[2], theta, size, 0.0, 0]).astype(float)
        source_param2 = np.array([0]).astype(float)
    elif source['method'] == 'structured_pattern': #structured light
        source_type = 4
        size = source['size']
        SL_xdim = source['pattern'].shape[1]
        SL_ydim = source['pattern'].shape[0]
        pattern_1D = source['pattern'].flatten()
        source_param1 = np.array([SL_xdim, SL_ydim, r0[2], nu0[0], nu0[1], nu0[2], 0.0, size, 0.0, 0]).astype(float)
        source_param2 = np.argwhere(pattern_1D == 1).flatten().astype(float)
    elif source['method'] == 'structured_pattern_cone':
        source_type = 5
        size = source['size']
        theta = source['theta']
        SL_xdim = source['pattern'].shape[1]
        SL_ydim = source['pattern'].shape[0]
        pattern_1D = source['pattern'].flatten()
        source_param1 = np.array([SL_xdim, SL_ydim, r0[2], nu0[0], nu0[1], nu0[2], theta, size, 0.0, 0]).astype(float)
        source_param2 = np.argwhere(pattern_1D == 1).flatten().astype(float)
    else:
        sys.exit("Source type is not supported")
            
    return source_type, source_param1, source_param2


# detector_params:
#      0,        1,            2,          3,         4,           5,            6,              7 
#    Type, Aperture size, focal_length, thickness,  Radius, refraction index, z_detector, Detector size(side of square)-only for lens
#
#    detector types: 0: bare sensor (only Aperture size is used),  
#                    1: lens
def getDetectorParams(detector, target):
    if 'type' not in detector:
        dtype = 0
    else:
        dtype = detector['type']
    
    if 'radius' not in detector:
        detR = 10.0
    else:
        detR = detector['radius']
    
    if 'z_detector' not in detector:
        zd = 10.0
    else:
        zd = detector['z_detector']    
    
    if 'focus_target' not in detector:
        if 'z_target' in target and target['type']>0:
            z0 = target['z_target']
        else:
            z0 = 10.0
    else:
        z0 = detector['focus_target']
        
    if 'det_size' not in detector:
        det_size = detR
    else:
        det_size = detector['det_size']
    
    if dtype == 0:
        detector_params = [0, detR, 0, 0, 0, 0, 0, 0]
    elif dtype == 1:
        R = 1.05 * detR
        thickness = 1.05 * (R - math.sqrt(R**2 - detR**2))
        f = 1/(1/zd + 1/z0)

        n = 1 + R/(2*f)

        A = 2*R - thickness
        B = 2*thickness - 2*R - R*R/f
        C = -thickness
        n = (-B + math.sqrt(B*B - 4*A*C)) / (2*A)

        if R <= thickness/2:
            print('Detector Params Error 1')
            return None
        if R < detR:
            print('Detector Params Error 1')
            return None
        if detR > math.sqrt(2*R*thickness - thickness**2):
            print('Detector Params Error 1')
            return None
        detector_params = [1, detR, f, thickness, R, n, zd, det_size]
    else:
        print('Undefined detector type')
    
    print ('Detector type: '+ str(detector_params[0]) +', Aperture size: ' + '{0:.1f}'.format(detector_params[1]) \
           + ', f: ' + '{0:.1f}'.format(detector_params[2])+ ', thickness: ' + '{0:.1f}'.format(detector_params[3])\
           +', R: ' + '{0:.1f}'.format(detector_params[4]) + ', n: ' + '{0:.1f}'.format(detector_params[5])\
           + ', z sensor: '+ '{0:.1f}'.format(detector_params[6]))   
    
    return detector_params






















###
# CPU versions, deprecated
###

# def lunchPacket(nPhotonsRequested = 1e6,
#                 nPhotonsToRun = 1e10,
#                 muS = 1.0, g = 0.85,
#                 source = {'r': np.array([0.0, 0.0, 0.0]),
#                           'mu': np.array([0.0, 0.0, 1.0]),
#                           'method': 'pencil', 'time_profile': 'delta'},
#                 detector = {'radius': 10.0},
#                 control_param = {'max_N': 1e5,
#                                  'max_distance_from_det': 1000},
#                 normalize_d = None,
#                 ret_cols = [1,2,7,8]
#                 ):
#     muS = float(muS)
#     g = float(g)
#     detR = float(detector['radius'])
#     max_N = control_param['max_N']
#     max_distance_from_det = float(control_param['max_distance_from_det'])
    
     
#     nPhotonsRequested = int(nPhotonsRequested)
#     data = np.ndarray(shape=(nPhotonsRequested, len(ret_cols)), dtype=float)
    
#     num_detected = 0
#     num_simulated = 0
    
#     while num_simulated < nPhotonsToRun and num_detected < nPhotonsRequested:
#         ret = initPhotonAndRun(muS = muS, g = g,
#                source = source,
#                detR = detR,
#                max_N = max_N,
#                max_distance_from_det = max_distance_from_det)
#         if ret[-1] >= 0:  #success
#             data[num_detected, :] = ret
#             num_detected += 1
#         num_simulated+=1
            
#     data = data[:num_detected, :] 
#     if normalize_d is not None:
#         data[:, 3] *= normalize_d
#     return data, num_simulated, num_detected






# def lunchBatch(batchSize = 1000,
#                muS  =  1.0, g = 0.85,
#                source = {'r': np.array([0.0, 0.0, 0.0]),
#                           'mu': np.array([0.0, 0.0, 1.0]),
#                           'method': 'pencil', 'time_profile': 'delta'},
#                detR = 1.0,
#                max_N = 1e5,
#                max_distance_from_det = 1000.0,
#                ret_cols = [1,2,7,8]):
    
    
#     # Assume all photons start the same (impulse pencil)
#     r0 = source['r']
#     nu0 = source['mu']
#     d0 = 0.0
#     n0 = 0        
#     arg = (muS, g, r0, nu0, d0, n0, detR, max_N, max_distance_from_det, ret_cols)
#     with Pool() as tp:
#         data = tp.starmap(propPhoton, repeat(arg, batchSize))
#     data = np.array(data)
    
# #    THIS IS A NON_PARALLEL HISTORIC VERSION
# #     data = np.zeros(shape=(batchSize, len(ret_cols)), dtype=float)
# #     for i in range(batchSize):
# #        #r0, nu0, d0, n0 = simSource(source)
# #        r0 = source['r']
# #        nu0 = source['mu']
# #        d0 = 0.0
# #        n0 = 0        
# #        data[i, :] = propPhoton(muS = muS, g = g,
# #                       r0  = r0,
# #                       nu0 = nu0,
# #                       d0 = d0, n0 = n0,
# #                       detR = detR,
# #                       max_N = max_N,
# #                       max_distance_from_det = max_distance_from_det,
# #                       ret_cols=ret_cols )

    
#     return data





# def initPhotonAndRun(muS  =  1.0, g = 0.85,
#                source = {'r': np.array([0.0, 0.0, 0.0]),
#                           'mu': np.array([0.0, 0.0, 1.0]),
#                           'method': 'pencil', 'time_profile': 'delta'},
#                detR = 1.0,
#                max_N = 1e5,
#                max_distance_from_det = 1000.0, 
#                ret_cols = [0,1,2,3,4,5,6,7,8]):
    
#     r0, nu0, d0, n0 = simSource(source)
    
#     return propPhoton(muS  =  muS, g = g,
#                r0  = r0,
#                nu0 = nu0,
#                d0 = d0, n0 = n0,
#                detR = detR,
#                max_N = max_N,
#                max_distance_from_det = max_distance_from_det,
#                ret_cols = ret_cols)





# #Return columns:
# #    0    1  2  3   4     5     6    7  8
# # Status, x ,y, z, mu_x, mu_y, mu_z, d, n
# def propPhoton(muS  =  1.0, g = 0.85,
#                r0  = np.array([0.0,0.0,0.0]),
#                nu0 = np.array([0.0,0.0,1.0]),
#                d0 = 0.0, n0 = 0,
#                detR = 1.0,
#                max_N = 1e5,
#                max_distance_from_det = 1000.0,
#                ret_cols = [1,2,7,8]):
#     np.random.seed()
#     r =  r0.copy()
#     nu = nu0.copy()
#     d = d0
#     n = n0        
    
#     detR2 = detR**2
#     ret = np.array([-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0 , -1.0, -1.0])
#     while True:
#         #print(r, nu, d, n)
#         # Calc random prop params
#         cd = - np.log(np.random.random()) / muS
        
#         t_r = r + cd * nu
        
#         #check if we hit the detector
#         if t_r[2] <= 0: #did we pass the detector?
#             cd = - r[2] / nu[2]
#             t_r = r + cd * nu
#             if t_r[0]**2 + t_r[1]**2 < detR2:
#                 d+=cd
#                 n+=1
#                 r = t_r
#                 ret = np.array([0, r[0],r[1],r[2],nu[0],nu[1],nu[2],d,n])
#                 break
#             else:  # if we passed the detector and didn't hit it we should restart
#                 ret[0] = 1
#                 break
            
#         # prop photon
#         for i in range(3):
#             r[i] = t_r[i] 
#         d += cd #prop distance
#         n += 1 #increase scatter counter
        
#         #scatter to new angle
#         psi = 2*np.pi*np.random.random()
#         mu = 1/(2*g) * (1+g**2 - ( (1-g*g)/(1-g+2*g*np.random.random()))**2)
        
#         sin_psi = np.sin(psi)
#         cos_psi = np.cos(psi)
#         sqrt_mu = np.sqrt(1-mu**2)
#         sqrt_w  = np.sqrt(1-nu[2]**2)
        
#         #update angels
#         if sqrt_w != 0:
#             prev_nu = nu.copy()            
#             nu[0] = prev_nu[0]*mu + (prev_nu[0]*prev_nu[2]*cos_psi - prev_nu[1]*sin_psi)*sqrt_mu/sqrt_w
#             nu[1] = prev_nu[1]*mu + (prev_nu[1]*prev_nu[2]*cos_psi + prev_nu[0]*sin_psi)*sqrt_mu/sqrt_w
#             nu[2] = prev_nu[2]*mu - cos_psi*sqrt_mu*sqrt_w
#         elif nu[2]==1.0:
#             nu[0] = sqrt_mu*cos_psi
#             nu[1] = sqrt_mu*sin_psi
#             nu[2] = mu
#         else: # nu[2]==-1.0
#             nu[0] = sqrt_mu*cos_psi
#             nu[1] = -sqrt_mu*sin_psi
#             nu[2] = -mu

#         #should we relaunch the photon?
#         if n >= max_N:
#             ret[0] = 2
#             break
#         if np.linalg.norm(r) > max_distance_from_det:  # assumes detector at origin
#             ret[0] = 3
#             break
#     ret = ret[ret_cols]
#     return ret