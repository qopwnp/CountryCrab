###
# Copyright (2024) Hewlett Packard Enterprise Development LP
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
###

import numpy as np
import pandas as pd
import os
import typing as t
import math

import campie
import cupy as cp
import warnings


def walksat_m(architecture, config, params):

    tcam = architecture[0]
    ram = architecture[1]
    tcam_cores = architecture[2]
    ram_cores = architecture[3]

    # get parameters. This should be "fixed values"
    # max runs is the number of parallel initialization (different inputs)
    max_runs = params.get("max_runs", 100)
    # max_flips is the maximum number of iterations
    max_flips = params.get("max_flips", 1000)
    # noise profile
    noise_dist = params.get("noise_distribution",'normal')
    # number of cores
    n_cores = params.get("n_cores", 1)
    # variables
    variables = tcam.shape[1]


    # get configuration. This is part of the scheduler search space
    # noise is the standard deviation of noise applied to the make_values
    noise = config.get('noise',0.8)

    
    # note, to speed up the code violated_constr_mat does not represent the violated constraints but the unsatisfied variables. It doesn't matter for the overall computation of p_vs_t
    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # generate random inputs
    inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips):
        n_iters += 1

        # global
        violated_clauses = campie.tcam_match(inputs, tcam)
        make_values = violated_clauses @ ram
        
        violated_constr = cp.sum(make_values > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break

        if n_cores == 1:
            # there is no difference between the global matches and the core matches (i.e. violated_clauses)
            # if there is only one core. we can just copy the global results and
            # and wrap a single core dimension around them
            violated_clauses, make_values, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [violated_clauses, make_values, violated_constr],
            )
        else:
            # otherwise, actually compute the matches (violated_clauses) for each core
            violated_clauses = campie.tcam_match(inputs, tcam_cores)
            make_values = violated_clauses @ ram_cores
            violated_constr = cp.sum(make_values > 0, axis=2)
        
        if noise_dist == 'normal':
            # add gaussian noise to the make values
            make_values += noise * cp.random.randn(*make_values.shape, dtype=make_values.dtype)  
        elif noise_dist == 'uniform':
            # add uniform noise. Note that the standard deviation is modulated by sqrt(3)
            make_values += cp.random.uniform(low=-noise*np.sqrt(3), high=noise*np.sqrt(3), size=make_values.shape, dtype=make_values.dtype) 
        elif noise_dist == 'intrinsic':
            # add noise considering automated annealing. Noise comes from memristor devices
            make_values += noise * cp.sqrt(make_values) * cp.random.randn(*make_values.shape, dtype=make_values.dtype)
        else:
            raise ValueError(f"Unknown noise distribution: {noise_dist}")

        # select highest values
        update = cp.argmax(make_values, axis=2)
        update[cp.where(violated_constr == 0)] = -1

        if n_cores == 1:
            # only one core, no need to do random picks
            update = update[0]
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]

        # update inputs
        campie.flip_indices(inputs, update[:, cp.newaxis])

    return violated_constr_mat, n_iters, inputs


def walksat_g(architecture, config, params):
    # print("Solving instance with walksat_g...")
    ramf_array = architecture[0]
    ramb_array = architecture[1]
    
    # get parameters. This should be "fixed values"
    # max runs is the number of parallel initialization (different inputs)
    max_runs = params.get("max_runs", 100)
    # max_flips is the maximum number of iterations
    max_flips = params.get("max_flips", 1000)
    # noise profile
    # number of cores
    n_cores = params.get("n_cores", 1)
    # variables
    clauses = ramf_array.shape[1]
    # clauses
    variables = int(ramf_array.shape[0]/2)
    literals = 2*variables

    # get configuration. This is part of the scheduler search space
    # noise is the standard deviation of noise applied to the make_values
    noise = config.get('noise',0.8)


    var_inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
    # do you need the following two?
    # pos_lit_indices = 2*cp.arange(0,variables,1)
    # neg_lit_indices = 2*cp.arange(0,variables,1)+1
    lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
    lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)


    ramf = cp.asarray(ramf_array, dtype=cp.float32)
    ramb = cp.asarray(ramb_array, dtype=cp.float32)
    

    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips - 1):
        n_iters += 1

        # global
        lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
        lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
        lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)
        f_val = lit_inputs @ ramf
        s_val = cp.zeros((max_runs,clauses))
        z_val = cp.zeros((max_runs,clauses))

        # s_val is the value of the unsatisfied clauses
        s_val[cp.where(f_val==0)] = 1
        # z_val is the value of satified clauses with only one literal = true
        z_val[cp.where(f_val==1)] = 1
        a = s_val @ ramb
        b = z_val @ ramb
        lit_one_indices = cp.where(lit_inputs==1)
        lit_zero_indices = cp.where(lit_inputs==0)
        # neg_lit_indices = 2*cp.arange(0,variables,1)+1 # do you need it?
        
        # compute the gain values
        mv_arr = cp.reshape(a[lit_zero_indices[0],lit_zero_indices[1]],(max_runs,variables))
        bv_arr = cp.reshape(b[lit_one_indices[0],lit_one_indices[1]],(max_runs,variables))
        g_arr = mv_arr - bv_arr
        y = g_arr
        violated_constr = cp.sum(mv_arr > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break 

        # add noise
        y += noise * cp.random.randn(*y.shape, dtype=y.dtype)
        y[mv_arr < 1] = -100

        # select highest values
        y, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [y, violated_constr],
            )
        update = cp.argmax(y, axis=2)
        update[cp.where(violated_constr == 0)] = -1
        

        if n_cores == 1:
            # only one core, no need to do random picks
            update = update[0]
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]
        campie.flip_indices(var_inputs, update[:, cp.newaxis])
    
    return violated_constr_mat, n_iters, var_inputs

def walksat_SKC(config: t.Dict, params: t.Dict) -> t.Union[t.Dict, t.Tuple]:
    warnings.warn("Untested heuristic with the broader environment. Please use walksat_m or walksat_g instead. ", UserWarning, stacklevel=2)
    # config contains parameters to optimize, params are fixed

    # Check GPUs are available.
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) is None:
        raise RuntimeError(
            f"No GPUs available. Please, set `CUDA_VISIBLE_DEVICES` environment variable."
        )
    #print('selected gpu')
    #print(os.environ.get("CUDA_VISIBLE_DEVICES", None))
    instance_addr = config["instance"]
    #print('loaded instance')
    ramf_array, ramb_array = map_camsat_g(instance_addr)
    #print('arrays compiled')
    max_runs = params.get("max_runs", 1000)

    clauses = ramf_array.shape[1]
    variables = int(ramf_array.shape[0]/2)
    literals = 2*variables
    var_inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
    pos_lit_indices = 2*cp.arange(0,variables,1)
    neg_lit_indices = 2*cp.arange(0,variables,1)+1
    lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
    lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)


    ramf = cp.asarray(ramf_array, dtype=cp.float32)
    ramb = cp.asarray(ramb_array, dtype=cp.float32)
    
    n_variables = variables
    n_words = clauses
    n_cores = config.get("n_cores", 1)

    task = params.get("task", "debug")

    if task == "solve":
        fname = params["hp_location"]
        optimized_hp = pd.read_csv(fname)
        if n_cores>1:
            filtered_df = optimized_hp[
                (optimized_hp["n_cores"] == n_cores)
                & (optimized_hp["n_words"] == n_words)
                & (optimized_hp["N_V"] == n_variables)
            ]
        else:
            filtered_df = optimized_hp[(optimized_hp["N_V"] == n_variables)]            
        noise = filtered_df["noise"].values[0]
        max_flips = int(filtered_df["max_flips_max"].values[0])
        max_flips_median = int(filtered_df["max_flips_median"].values[0])

    else:
        noise = config.get("noise", 2)
        max_flips = params.get("max_flips", 1000)

    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips - 1):
        n_iters += 1

        # global
        lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
        lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
        lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)
        f_val = lit_inputs @ ramf
        s_val = cp.zeros((max_runs,clauses))
        z_val = cp.zeros((max_runs,clauses))
        s_val[cp.where(f_val==0)] = 1
        z_val[cp.where(f_val==1)] = 1
        a = s_val @ ramb
        b = z_val @ ramb
        lit_one_indices = cp.where(lit_inputs==1)
        lit_zero_indices = cp.where(lit_inputs==0)
        neg_lit_indices = 2*cp.arange(0,variables,1)+1
        mv_arr = cp.reshape(a[lit_zero_indices[0],lit_zero_indices[1]],(max_runs,variables))
        bv_arr = cp.reshape(b[lit_one_indices[0],lit_one_indices[1]],(max_runs,variables))
        g_arr = mv_arr - bv_arr
        y = g_arr
        violated_constr = cp.sum(mv_arr > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break 

        #if n_cores == 1:
            # there is no difference between the global matches and the core matches
            # if there is only one core. we can just copy the global results and
            # and wrap a single core dimension around them
         #   matches, y, violated_constr = map(
          #      lambda x: x[cp.newaxis, :],
           #     [matches, y, violated_constr],
            #)
        #else:
            # otherwise, actually compute the matches for each core
         #   matches = campie.tcam_match(inputs, tcam_cores)
          #  y = matches @ ram_cores
           # violated_constr = cp.sum(y > 0, axis=2)

        # add noise
        #print(mv_arr)
        #print(y)
        tmp1 = mv_arr<1
        tmp2 = bv_arr==0
        #tmp3 = tmp2.any(1)
        tmp3 = (~tmp1&tmp2).any(1)
        tmp4 = cp.repeat(cp.reshape(tmp3,(max_runs,1)),variables,axis=1)
        tmp5 = (tmp4 & (tmp1 | ~tmp2)) | (~tmp4 & tmp1)
        y += noise * cp.random.randn(*y.shape, dtype=y.dtype)
        #print(y)
        #y[mv_arr < 1] = -100

        
        #print("MV == 0 Indices: ",tmp1)
        #print("BV == 0 Indices: ",tmp2)
        #print("Atleast one BV==0: ",tmp3)
        #print("Final complement Indices: ",tmp5)
        #print(y)
        y[tmp5] = -1000
        #print("MV: ",mv_arr)
        #print("BV: ",bv_arr)
        #print("G+noise: ",y)
        # select highest values
        y, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [y, violated_constr],
            )
        update = cp.argmax(y, axis=2)
        update[cp.where(violated_constr == 0)] = -1
        

        if n_cores == 1:
            # only one core, no need to do random picks
            update = update[0]
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]
        #print(update)
        # update inputs
        #campie.flip_indices(var_inputs, update[:, cp.newaxis])
        campie.flip_indices(var_inputs, update[:, cp.newaxis])
    
    return violated_constr_mat, n_iters, var_inputs

def walksat_Gseq(config: t.Dict, params: t.Dict) -> t.Union[t.Dict, t.Tuple]:
    warnings.warn("Untested heuristic with the broader environment. Please use walksat_m or walksat_g instead. ", UserWarning, stacklevel=2)
    # config contains parameters to optimize, params are fixed

    # Check GPUs are available.
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) is None:
        raise RuntimeError(
            f"No GPUs available. Please, set `CUDA_VISIBLE_DEVICES` environment variable."
        )
    #print('selected gpu')
    #print(os.environ.get("CUDA_VISIBLE_DEVICES", None))
    instance_addr = config["instance"]
    #print('loaded instance')
    ramf_array, ramb_array = map_camsat_g(instance_addr)
    #print('arrays compiled')
    max_runs = params.get("max_runs", 1000)

    clauses = ramf_array.shape[1]
    variables = int(ramf_array.shape[0]/2)
    literals = 2*variables
    var_inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
    pos_lit_indices = 2*cp.arange(0,variables,1)
    neg_lit_indices = 2*cp.arange(0,variables,1)+1
    lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
    lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)


    ramf = cp.asarray(ramf_array, dtype=cp.float32)
    ramb = cp.asarray(ramb_array, dtype=cp.float32)
    
    n_variables = variables
    n_words = clauses
    n_cores = config.get("n_cores", 1)

    task = params.get("task", "debug")

    if task == "solve":
        fname = params["hp_location"]
        optimized_hp = pd.read_csv(fname)
        if n_cores>1:
            filtered_df = optimized_hp[
                (optimized_hp["n_cores"] == n_cores)
                & (optimized_hp["n_words"] == n_words)
                & (optimized_hp["N_V"] == n_variables)
            ]
        else:
            filtered_df = optimized_hp[(optimized_hp["N_V"] == n_variables)]            
        noise = filtered_df["noise"].values[0]
        max_flips = int(filtered_df["max_flips_max"].values[0])
        max_flips_median = int(filtered_df["max_flips_median"].values[0])

    else:
        noise = config.get("noise", 2)
        max_flips = params.get("max_flips", 1000)

    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips - 1):
        n_iters += 1

        # global
        lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
        lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
        lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)
        f_val = lit_inputs @ ramf
        s_val = cp.zeros((max_runs,clauses))
        z_val = cp.zeros((max_runs,clauses))
        s_val[cp.where(f_val==0)] = 1
        z_val[cp.where(f_val==1)] = 1
        a = s_val @ ramb
        b = z_val @ ramb
        lit_one_indices = cp.where(lit_inputs==1)
        lit_zero_indices = cp.where(lit_inputs==0)
        neg_lit_indices = 2*cp.arange(0,variables,1)+1
        mv_arr = cp.reshape(a[lit_zero_indices[0],lit_zero_indices[1]],(max_runs,variables))
        bv_arr = cp.reshape(b[lit_one_indices[0],lit_one_indices[1]],(max_runs,variables))
        g_arr = mv_arr - bv_arr
        y = g_arr
        violated_constr = cp.sum(mv_arr > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break 

        #if n_cores == 1:
            # there is no difference between the global matches and the core matches
            # if there is only one core. we can just copy the global results and
            # and wrap a single core dimension around them
         #   matches, y, violated_constr = map(
          #      lambda x: x[cp.newaxis, :],
           #     [matches, y, violated_constr],
            #)
        #else:
            # otherwise, actually compute the matches for each core
         #   matches = campie.tcam_match(inputs, tcam_cores)
          #  y = matches @ ram_cores
           # violated_constr = cp.sum(y > 0, axis=2)

        # add noise
        #print(mv_arr)
        #print(y)
        #y += noise * cp.random.randn(*y.shape, dtype=y.dtype)
        #print(y)
        y[mv_arr < 1] = -100

        all_variable_indices = cp.reshape(cp.arange(0,variables,1),(1,variables))
        all_variable_indices = cp.repeat(all_variable_indices,max_runs,axis=0)
        all_variable_indices[mv_arr < 1] = -1
        all_variable_indices_sorted = cp.sort(-all_variable_indices,axis=1)
        all_variable_indices_sorted = -all_variable_indices_sorted

        #print(violated_constr)
        violated_constr_temp = cp.copy(violated_constr)
        violated_constr_temp[violated_constr == 0] = 1
        num_candidate_variables = cp.array(np.random.randint(0,cp.asnumpy(violated_constr_temp)))
        xy = cp.arange(0,max_runs,1)
        update2 = all_variable_indices_sorted[xy,num_candidate_variables]
        
        #print(y)
        # select highest values
        y, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [y, violated_constr],
            )
        update = cp.argmax(y, axis=2)
        #print(update)
        update[cp.where(violated_constr == 0)] = -1
        update1 = update[0]
        
        
        ind1 = cp.random.uniform(0,1,max_runs)>noise
        ind2 = update1==-1
        ind3 = ind1 & ~ind2
        update1[ind3] = update2[ind3]
        #print("Final Update: ",update1)
        
        
        if n_cores == 1:
            # only one core, no need to do random picks
            #update = update[0]
            update = update1
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]
        #print(update)
        # update inputs
        #campie.flip_indices(var_inputs, update[:, cp.newaxis])
        campie.flip_indices(var_inputs, update[:, cp.newaxis])

    return violated_constr_mat, n_iters, var_inputs

def walksat_SKCseq(config: t.Dict, params: t.Dict) -> t.Union[t.Dict, t.Tuple]:
    warnings.warn("Untested heuristic with the broader environment. Please use walksat_m or walksat_g instead. ", UserWarning, stacklevel=2)
    # config contains parameters to optimize, params are fixed

    # Check GPUs are available.
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) is None:
        raise RuntimeError(
            f"No GPUs available. Please, set `CUDA_VISIBLE_DEVICES` environment variable."
        )
    #print('selected gpu')
    #print(os.environ.get("CUDA_VISIBLE_DEVICES", None))
    instance_addr = config["instance"]
    #print('loaded instance')
    ramf_array, ramb_array = map_camsat_g(instance_addr)
    #print('arrays compiled')
    max_runs = params.get("max_runs", 1000)

    clauses = ramf_array.shape[1]
    variables = int(ramf_array.shape[0]/2)
    literals = 2*variables
    var_inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
    pos_lit_indices = 2*cp.arange(0,variables,1)
    neg_lit_indices = 2*cp.arange(0,variables,1)+1
    lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
    lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)


    ramf = cp.asarray(ramf_array, dtype=cp.float32)
    ramb = cp.asarray(ramb_array, dtype=cp.float32)
    
    n_variables = variables
    n_words = clauses
    n_cores = config.get("n_cores", 1)

    task = params.get("task", "debug")

    if task == "solve":
        fname = params["hp_location"]
        optimized_hp = pd.read_csv(fname)
        if n_cores>1:
            filtered_df = optimized_hp[
                (optimized_hp["n_cores"] == n_cores)
                & (optimized_hp["n_words"] == n_words)
                & (optimized_hp["N_V"] == n_variables)
            ]
        else:
            filtered_df = optimized_hp[(optimized_hp["N_V"] == n_variables)]            
        noise = filtered_df["noise"].values[0]
        max_flips = int(filtered_df["max_flips_max"].values[0])
        max_flips_median = int(filtered_df["max_flips_median"].values[0])

    else:
        noise = config.get("noise", 2)
        max_flips = params.get("max_flips", 1000)

    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips - 1):
        n_iters += 1

        # global
        lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
        lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
        lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)
        f_val = lit_inputs @ ramf
        s_val = cp.zeros((max_runs,clauses))
        z_val = cp.zeros((max_runs,clauses))
        s_val[cp.where(f_val==0)] = 1
        z_val[cp.where(f_val==1)] = 1
        a = s_val @ ramb
        b = z_val @ ramb
        lit_one_indices = cp.where(lit_inputs==1)
        lit_zero_indices = cp.where(lit_inputs==0)
        neg_lit_indices = 2*cp.arange(0,variables,1)+1
        mv_arr = cp.reshape(a[lit_zero_indices[0],lit_zero_indices[1]],(max_runs,variables))
        bv_arr = cp.reshape(b[lit_one_indices[0],lit_one_indices[1]],(max_runs,variables))
        g_arr = mv_arr - bv_arr
        y = g_arr
        violated_constr = cp.sum(mv_arr > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break 

        #if n_cores == 1:
            # there is no difference between the global matches and the core matches
            # if there is only one core. we can just copy the global results and
            # and wrap a single core dimension around them
         #   matches, y, violated_constr = map(
          #      lambda x: x[cp.newaxis, :],
           #     [matches, y, violated_constr],
            #)
        #else:
            # otherwise, actually compute the matches for each core
         #   matches = campie.tcam_match(inputs, tcam_cores)
          #  y = matches @ ram_cores
           # violated_constr = cp.sum(y > 0, axis=2)

        # add noise
        #print(mv_arr)
        #print(y)
        #y += noise * cp.random.randn(*y.shape, dtype=y.dtype)
        #print(y)
        

        tmp1 = mv_arr<1
        tmp2 = bv_arr==0
        tmp3 = (~tmp1&tmp2).any(1)
        tmp4 = cp.repeat(cp.reshape(tmp3,(max_runs,1)),variables,axis=1)
        tmp5 = (tmp4 & (tmp1 | ~tmp2)) | (~tmp4 & tmp1)

        y[tmp5] = -1000
        
        all_variable_indices = cp.reshape(cp.arange(0,variables,1),(1,variables))
        all_variable_indices = cp.repeat(all_variable_indices,max_runs,axis=0)
        all_variable_indices[tmp5] = -1
        all_variable_indices_sorted = cp.sort(-all_variable_indices,axis=1)
        all_variable_indices_sorted = -all_variable_indices_sorted

        #print(violated_constr)
        tmp6 = cp.ones((max_runs,variables))
        tmp6[tmp5] = 0
        violated_constr_temp = cp.sum(tmp6,axis=1)
        violated_constr_temp[violated_constr_temp == 0] = 1
        num_candidate_variables = cp.array(np.random.randint(0,cp.asnumpy(violated_constr_temp)))
        xy = cp.arange(0,max_runs,1)
        update2 = all_variable_indices_sorted[xy,num_candidate_variables]
        
        #print(y)
        # select highest values
        y, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [y, violated_constr],
            )
        update = cp.argmax(y, axis=2)
        update[cp.where(violated_constr == 0)] = -1
        update1 = update[0]
        
        ind1 = cp.random.uniform(0,1,max_runs)>noise
        ind2 = update1==-1
        ind3 = ind1 & ~ind2
        update1[ind3] = update2[ind3]
        #print("Final Update: ",update1)
        
        
        if n_cores == 1:
            # only one core, no need to do random picks
            #update = update[0]
            update = update1
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]
        #print(update)
        # update inputs
        #campie.flip_indices(var_inputs, update[:, cp.newaxis])
        campie.flip_indices(var_inputs, update[:, cp.newaxis])
    
    return violated_constr_mat, n_iters, var_inputs

def walksat_B2seq(config: t.Dict, params: t.Dict) -> t.Union[t.Dict, t.Tuple]:
    warnings.warn("Untested heuristic with the broader environment. Please use walksat_m or walksat_g instead. ", UserWarning, stacklevel=2)
    # config contains parameters to optimize, params are fixed

    # Check GPUs are available.
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) is None:
        raise RuntimeError(
            f"No GPUs available. Please, set `CUDA_VISIBLE_DEVICES` environment variable."
        )
    #print('selected gpu')
    #print(os.environ.get("CUDA_VISIBLE_DEVICES", None))
    instance_addr = config["instance"]
    #print('loaded instance')
    ramf_array, ramb_array = map_camsat_g(instance_addr)
    #print('arrays compiled')
    max_runs = params.get("max_runs", 1000)

    clauses = ramf_array.shape[1]
    variables = int(ramf_array.shape[0]/2)
    literals = 2*variables
    var_inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
    pos_lit_indices = 2*cp.arange(0,variables,1)
    neg_lit_indices = 2*cp.arange(0,variables,1)+1
    lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
    lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)


    ramf = cp.asarray(ramf_array, dtype=cp.float32)
    ramb = cp.asarray(ramb_array, dtype=cp.float32)
    
    n_variables = variables
    n_words = clauses
    n_cores = config.get("n_cores", 1)

    task = params.get("task", "debug")

    if task == "solve":
        fname = params["hp_location"]
        optimized_hp = pd.read_csv(fname)
        if n_cores>1:
            filtered_df = optimized_hp[
                (optimized_hp["n_cores"] == n_cores)
                & (optimized_hp["n_words"] == n_words)
                & (optimized_hp["N_V"] == n_variables)
            ]
        else:
            filtered_df = optimized_hp[(optimized_hp["N_V"] == n_variables)]            
        noise = filtered_df["noise"].values[0]
        max_flips = int(filtered_df["max_flips_max"].values[0])
        max_flips_median = int(filtered_df["max_flips_median"].values[0])

    else:
        noise = config.get("noise", 2)
        max_flips = params.get("max_flips", 1000)

    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)

    # tracks the amount of iteratiosn that are actually completed
    n_iters = 0

    for it in range(max_flips - 1):
        n_iters += 1

        # global
        lit_inputs = cp.zeros((max_runs,literals)).astype(cp.float32)
        lit_inputs[:,2*cp.arange(0,variables,1)]=var_inputs
        lit_inputs[:,2*cp.arange(0,variables,1)+1]=cp.abs(var_inputs-1)
        f_val = lit_inputs @ ramf
        s_val = cp.zeros((max_runs,clauses))
        z_val = cp.zeros((max_runs,clauses))
        s_val[cp.where(f_val==0)] = 1
        z_val[cp.where(f_val==1)] = 1
        a = s_val @ ramb
        b = z_val @ ramb
        lit_one_indices = cp.where(lit_inputs==1)
        lit_zero_indices = cp.where(lit_inputs==0)
        neg_lit_indices = 2*cp.arange(0,variables,1)+1
        mv_arr = cp.reshape(a[lit_zero_indices[0],lit_zero_indices[1]],(max_runs,variables))
        bv_arr = cp.reshape(b[lit_one_indices[0],lit_one_indices[1]],(max_runs,variables))
        g_arr = mv_arr - bv_arr
        y = bv_arr
        violated_constr = cp.sum(mv_arr > 0, axis=1)
        violated_constr_mat[:, it] = violated_constr

        # early stopping
        if cp.sum(violated_constr_mat[:, it]) == 0:
            break 

        #if n_cores == 1:
            # there is no difference between the global matches and the core matches
            # if there is only one core. we can just copy the global results and
            # and wrap a single core dimension around them
         #   matches, y, violated_constr = map(
          #      lambda x: x[cp.newaxis, :],
           #     [matches, y, violated_constr],
            #)
        #else:
            # otherwise, actually compute the matches for each core
         #   matches = campie.tcam_match(inputs, tcam_cores)
          #  y = matches @ ram_cores
           # violated_constr = cp.sum(y > 0, axis=2)

        # add noise
        #print(mv_arr)
        #print(y)
        #y += noise * cp.random.randn(*y.shape, dtype=y.dtype)
        #print(y)
        

        tmp1 = mv_arr<1
        tmp2 = bv_arr==0
        tmp3 = (~tmp1&tmp2).any(1)
        tmp4 = cp.repeat(cp.reshape(tmp3,(max_runs,1)),variables,axis=1)
        tmp5 = (tmp4 & (tmp1 | ~tmp2)) | (~tmp4 & tmp1)

        y[tmp5] = 1000
        
        all_variable_indices = cp.reshape(cp.arange(0,variables,1),(1,variables))
        all_variable_indices = cp.repeat(all_variable_indices,max_runs,axis=0)
        all_variable_indices[tmp5] = -1
        all_variable_indices_sorted = cp.sort(-all_variable_indices,axis=1)
        all_variable_indices_sorted = -all_variable_indices_sorted

        #print(violated_constr)
        tmp6 = cp.ones((max_runs,variables))
        tmp6[tmp5] = 0
        violated_constr_temp = cp.sum(tmp6,axis=1)
        violated_constr_temp[violated_constr_temp == 0] = 1
        num_candidate_variables = cp.array(np.random.randint(0,cp.asnumpy(violated_constr_temp)))
        xy = cp.arange(0,max_runs,1)
        update2 = all_variable_indices_sorted[xy,num_candidate_variables]
        
        #print(y)
        # select highest values
        y, violated_constr = map(
                lambda x: x[cp.newaxis, :],
                [y, violated_constr],
            )
        update = cp.argmin(y, axis=2)
        update[cp.where(violated_constr == 0)] = -1
        update1 = update[0]
        
        ind1 = cp.random.uniform(0,1,max_runs)>noise
        ind2 = update1==-1
        ind3 = ind1 & ~ind2 & ~tmp3
        update1[ind3] = update2[ind3]
        #print("Final Update: ",update1)
        
        
        if n_cores == 1:
            # only one core, no need to do random picks
            #update = update[0]
            update = update1
        else:
            # reduction -> randomly selecting one update
            update = update.T
            random_indices = cp.random.randint(0, update.shape[1], size=update.shape[0])
            update = update[cp.arange(update.shape[0]), random_indices]
        #print(update)
        # update inputs
        #campie.flip_indices(var_inputs, update[:, cp.newaxis])
        campie.flip_indices(var_inputs, update[:, cp.newaxis])
    
    return violated_constr_mat, n_iters, var_inputs