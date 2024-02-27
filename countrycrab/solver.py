import numpy as np
import pandas as pd
import os
import typing as t
import math

from countrycrab.compiler import compile_walksat_m
from countrycrab.analyze import vector_its
from countrycrab.heuristics import walksat_m

import campie
import cupy as cp


def solve(config: t.Dict, params: t.Dict) -> t.Union[t.Dict, t.Tuple]:
    # config contains parameters to optimize, params are fixed

    # Check GPUs are available.
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) is None:
        raise RuntimeError(
            f"No GPUs available. Please, set `CUDA_VISIBLE_DEVICES` environment variable."
        )


    # get configuration. This is part of the scheduler search space
    # noise is the standard deviation of noise applied to the make_values
    noise = config.get("noise", 0.5)

    # load instance and map it to the CAMSAT arrays
    #tcam_array, ram_array = map_camsat(instance_addr)
    #clauses = tcam_array.shape[0]
    #variables = tcam_array.shape[1]

    # get parameters. This should be "fixed values"
    # max runs is the number of parallel initialization (different inputs)
    max_runs = params.get("max_runs", 100)
    # max_flips is the maximum number of iterations
    max_flips = params.get("max_flips", 1000)
    # noise profile
    noise_dist = params.get("noise_distribution",'normal')
    # target_probability
    p_solve = params.get("p_solve", 0.99)
    # task is the type of task to be performed
    task = params.get("task", "debug")

    compiler_name = config.get("compiler", 'compile_walksat_m')
    
    compilers_dict = {
        'compile_walksat_m': compile_walksat_m,
    }

    compiler_function = compilers_dict.get(compiler_name)

    tcam, ram, tcam_cores, ram_cores, n_cores = compiler_function(config, params)
    variables = tcam.shape[1]
    clauses = tcam.shape[0]

    # generate random inputs
    inputs = cp.random.randint(2, size=(max_runs, variables)).astype(cp.float32)
    
    # hyperparemeters can be provided by the user
    if "hp_location" in params:
        optimized_hp = pd.read_csv(params["hp_location"])
        # take the correct hp for the size of the problem
        filtered_df = optimized_hp[(optimized_hp["N_V"] == variables)]            
        noise = filtered_df["noise"].values[0]
        max_flips = int(filtered_df["max_flips_max"].values[0])


    
    # note, to speed up the code violated_constr_mat does not represent the violated constraints but the unsatisfied variables. It doesn't matter for the overall computation of p_vs_t
    violated_constr_mat = cp.full((max_runs, max_flips), cp.nan, dtype=cp.float32)
    
    # load the heuristic function from a separate file
    heuristic_name = config.get("heuristic", 'walksat_m')
    
    heuristics_dict = {
        'walksat-m': walksat_m,
    }

    heuristic_function = heuristics_dict.get(heuristic_name)

    if heuristic_function is None:
        raise ValueError(f"Unknown heuristic: {heuristic_name}")

    # call the heuristic function with the necessary arguments
    data = {
        "inputs": inputs,
        "tcam": tcam,
        "ram": ram,
        "tcam_cores": tcam_cores,
        "ram_cores": ram_cores,
        "violated_constr_mat": violated_constr_mat,
        "max_flips": max_flips,
        "n_cores": n_cores,
        "noise": noise,
        "noise_dist": noise_dist,
    }
    violated_constr_mat,n_iters = heuristic_function(data)

    
    # probability os solving the problem as a function of the iterations
    p_vs_t = cp.sum(violated_constr_mat[:, 1 : n_iters + 1] == 0, axis=0) / max_runs
    p_vs_t = cp.asnumpy(p_vs_t)
    # check if the problem was solved at least one
    solved = (np.sum(p_vs_t) > 0)

    # Compute iterations to solution for 99% of probability to solve the problem
    iteration_vector = np.arange(1, n_iters + 1)
    its = vector_its(iteration_vector, p_vs_t, p_target=p_solve)

    if task == 'hpo':
        if solved:
            # return the best (minimum) its and the corresponding max_flips
            best_its = np.min(its[its > 0])
            best_max_flips = np.where(its == its[its > 0][np.argmin(its[its > 0])])
            return {"its_opt": best_its, "max_flips_opt": best_max_flips[0][0]}
        else:
            return {"its_opt": np.nan, "max_flips_opt": max_flips}
    
    elif task == 'solve':
        if solved:
            # return the its at the given max_flips
            return {"its": its[max_flips]}
        else:
            return {"its": np.nan}
    elif task == "debug":
        inputs = cp.asnumpy(inputs)
        return p_vs_t, cp.asnumpy(violated_constr_mat), cp.asnumpy(inputs)
    else:
        raise ValueError(f"Unknown task: {task}")