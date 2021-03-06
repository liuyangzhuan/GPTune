# GPTune Copyright (c) 2019, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory (subject to receipt of any
# required approvals from the U.S.Dept. of Energy) and the University of
# California, Berkeley.  All rights reserved.
#
# If you have questions about your rights to use or distribute this software, 
# please contact Berkeley Lab's Intellectual Property Office at IPO@lbl.gov.
#
# NOTICE. This Software was developed under funding from the U.S. Department 
# of Energy and the U.S. Government consequently retains certain rights.
# As such, the U.S. Government has been granted for itself and others acting
# on its behalf a paid-up, nonexclusive, irrevocable, worldwide license in
# the Software to reproduce, distribute copies to the public, prepare
# derivative works, and perform publicly and display publicly, and to permit
# other to do so.
#

import abc
from typing import Collection, Tuple
import numpy as np

from problem import Problem
from computer import Computer
from data import Data

import mpi4py
from mpi4py import MPI
from mpi4py import futures


import concurrent
from concurrent import futures						  
class Model(abc.ABC):

    def __init__(self, problem : Problem, computer : Computer, **kwargs):

        self.problem = problem
        self.computer = computer
        self.M = None

    @abc.abstractmethod
    def train(self, data : Data, **kwargs):

        raise Exception("Abstract method")

    @abc.abstractmethod
    def update(self, newdata : Data, do_train: bool = False, **kwargs):

        raise Exception("Abstract method")

    @abc.abstractmethod
    def predict(self, points : Collection[np.ndarray], tid : int, **kwargs) -> Collection[Tuple[float, float]]:

        raise Exception("Abstract method")


import GPy

class Model_GPy_LCM(Model):

#model_threads=1
#model_processes=1
#model_groups=1
#model_restarts=1
#model_max_iters=15000
#model_latent=0
#model_sparse=False
#model_inducing=None
#model_layers=2

    def train(self, data : Data, **kwargs):

        multitask = len(data.T) > 1

        if (kwargs['model_latent'] is None):
            model_latent = data.NI
        else:
            model_latent = kwargs['model_latent']

        if (kwargs['model_sparse'] and kwargs['model_inducing'] is None):
            if (multitask):
                lenx = sum([len(X) for X in data.X])
            else:
                lenx = len(data.X)
            model_inducing = int(min(lenx, 3 * np.sqrt(lenx)))

        if (multitask):
            kernels_list = [GPy.kern.RBF(input_dim = self.problem.DP, ARD=True) for k in range(model_latent)]
            K = GPy.util.multioutput.LCM(input_dim = self.problem.DP, num_outputs = data.NI, kernels_list = kernels_list, W_rank = 1, name='GPy_LCM')
            if (kwargs['model_sparse']):
                self.M = GPy.models.SparseGPCoregionalizedRegression(X_list = data.X, Y_list = data.Y, kernel = K, num_inducing = model_inducing)
            else:
                self.M = GPy.models.GPCoregionalizedRegression(X_list = data.X, Y_list = data.Y, kernel = K)
        else:
            K = GPy.kern.RBF(input_dim = self.problem.DP, ARD=True, name='GPy_GP')
            if (kwargs['model_sparse']):
                self.M = GPy.models.SparseGPRegression(data.X[0], data.Y[0], kernel = K, num_inducing = model_inducing)
            else:
                self.M = GPy.models.GPRegression(data.X[0], data.Y[0], kernel = K)
            
#        np.random.seed(mpi_rank)
#        num_restarts = max(1, model_n_restarts // mpi_size)

        resopt = self.M.optimize_restarts(num_restarts = kwargs['model_restarts'], robust = True, verbose = kwargs['verbose'], parallel = (kwargs['model_threads'] > 1), num_processes = kwargs['model_threads'], messages = "True", optimizer = 'lbfgs', start = None, max_iters = kwargs['model_max_iters'], ipython_notebook = False, clear_after_finish = True)

#        self.M.param_array[:] = allreduce_best(self.M.param_array[:], resopt)[:]
        self.M.parameters_changed()

        return

    def update(self, newdata : Data, do_train: bool = False, **kwargs):

        #XXX TODO
        self.train(newdata, **kwargs)

    def predict(self, points : Collection[np.ndarray], tid : int, **kwargs) -> Collection[Tuple[float, float]]:

        x = np.empty((1, points.shape[0] + 1))
        x[0,:-1] = points
        x[0,-1] = tid
        (mu, var) = self.M.predict_noiseless(x)

        return (mu, var)


from lcm import LCM

class Model_LCM(Model):

	def train(self, data : Data, **kwargs):

		self.train_mpi(data, i_am_manager = True, restart_iters=list(range(kwargs['model_restarts'])), **kwargs)

	def train_mpi(self, data : Data, i_am_manager : bool, restart_iters : Collection[int] = None, **kwargs):

		if (kwargs['model_latent'] is None):
			Q = data.NI
		else:
			Q = kwargs['model_latent']

		if (kwargs['distributed_memory_parallelism'] and i_am_manager): 
			mpi_comm = self.computer.spawn(__file__, kwargs['model_restart_processes'], kwargs['model_restart_threads'], kwargs=kwargs) # XXX add args and kwargs
			kwargs_tmp = kwargs
			# print("kwargs_tmp",kwargs_tmp)
   
   
			if "mpi_comm" in kwargs_tmp:
				del kwargs_tmp["mpi_comm"]   # mpi_comm is not picklable
			_ = mpi_comm.bcast((self, data, restart_iters, kwargs_tmp), root=mpi4py.MPI.ROOT)
			tmpdata = mpi_comm.gather(None, root=mpi4py.MPI.ROOT)
			mpi_comm.Disconnect()
			res=[]
			for p in range(int(kwargs['model_restart_processes'])):
				res = res + tmpdata[p]

		elif (kwargs['shared_memory_parallelism']): #YL: not tested 

			#with concurrent.futures.ProcessPoolExecutor(max_workers = kwargs['search_multitask_threads']) as executor:
			with concurrent.futures.ThreadPoolExecutor(max_workers = kwargs['model_restart_threads']) as executor:
				def fun(restart_iter):
					if ('seed' in kwargs):
						seed = kwargs['seed'] * kwargs['model_restart_threads'] + restart_iter
					else:
						seed = restart_iter
					np.random.seed(seed)
					kern = LCM(input_dim = self.problem.DP, num_outputs = data.NI, Q = Q)
					if (restart_iter == 0 and self.M is not None):
						kern.set_param_array(self.M.kern.get_param_array())
					return kern.train_kernel(X = data.X, Y = data.Y, computer = self.computer, kwargs = kwargs)
				res = list(executor.map(fun, restart_iters, timeout=None, chunksize=1))

		else:
			def fun(restart_iter):
				np.random.seed(restart_iter)
				kern = LCM(input_dim = self.problem.DP, num_outputs = data.NI, Q = Q)
				# print('I am here')
				return kern.train_kernel(X = data.X, Y = data.Y, computer = self.computer, kwargs = kwargs)
			res = list(map(fun, restart_iters))

		if (kwargs['distributed_memory_parallelism'] and i_am_manager == False): 
			return res
		
		# print("zenhui",self.problem.DP,data.NI,Q)
		kern = LCM(input_dim = self.problem.DP, num_outputs = data.NI, Q = Q)
		bestxopt = min(res, key = lambda x: x[1])[0]
		kern.set_param_array(bestxopt)

		# YL: why sigma is enough to compute the likelihood, see https://gpy.readthedocs.io/en/deploy/GPy.likelihoods.html 			
		likelihoods_list = [GPy.likelihoods.Gaussian(variance = kern.sigma[i], name = "Gaussian_noise_%s" %i) for i in range(data.NI)]
		self.M = GPy.models.GPCoregionalizedRegression(data.X, data.Y, kern, likelihoods_list = likelihoods_list)

		return

	def update(self, newdata : Data, do_train: bool = False, **kwargs):

		#XXX TODO
		self.train(newdata, **kwargs)

	def predict(self, points : Collection[np.ndarray], tid : int, **kwargs) -> Collection[Tuple[float, float]]:

		x = np.empty((1, points.shape[0] + 1))
		x[0,:-1] = points
		x[0,-1] = tid
		(mu, var) = self.M.predict_noiseless(x)

		return (mu, var)


class Model_DGP(Model):

    def train(self, data : Data, **kwargs):

        multitask = len(self.T) > 1

        if (multitask):
            X = np.array([np.concatenate((self.T[i], self.X[i][j])) for i in range(len(self.T)) for j in range(self.X[i].shape[0])])
        else:
            X = self.X[0]
        Y = np.array(list(itertools.chain.from_iterable(self.Y)))

        #--------- Model Construction ----------#
        model_n_layers = 2
        # Define what kernels to use per layer
        kerns = [GPy.kern.RBF(input_dim=Q, ARD=True) + GPy.kern.Bias(input_dim=Q) for lev in range(model_n_layers)]
        kerns.append(GPy.kern.RBF(input_dim=X.shape[1], ARD=True) + GPy.kern.Bias(input_dim=X.shape[1]))
        # Number of inducing points to use
        if (num_inducing is None):
            if (multitask):
                lenx = sum([len(X) for X in self.X])
            else:
                lenx = len(self.X)
#            num_inducing = int(min(lenx, 3 * np.sqrt(lenx)))
            num_inducing = lenx
        # Whether to use back-constraint for variational posterior
        back_constraint = False
        # Dimensions of the MLP back-constraint if set to true
        encoder_dims=[[X.shape[0]],[X.shape[0]],[X.shape[0]]]

        nDims = [Y.shape[1]] + model_n_layers * [Q] + [X.shape[1]]
#        self.M = deepgp.DeepGP(nDims, Y, X=X, num_inducing=num_inducing, likelihood = None, inits='PCA', name='deepgp', kernels=kerns, obs_data='cont', back_constraint=True, encoder_dims=encoder_dims, mpi_comm=mpi_comm, self.mpi_root=0, repeatX=False, inference_method=None)#, **kwargs):
        self.M = deepgp.DeepGP(nDims, Y, X=X, num_inducing=num_inducing, likelihood = None, inits='PCA', name='deepgp', kernels=kerns, obs_data='cont', back_constraint=False, encoder_dims=None, mpi_comm=None, mpi_root=0, repeatX=False, inference_method=None)#, **kwargs):
#        self.M = deepgp.DeepGP([Y.shape[1], Q, Q, X.shape[1]], Y, X=X, kernels=[kern1, kern2, kern3], num_inducing=num_inducing, back_constraint=back_constraint)

        #--------- Optimization ----------#
        # Make sure initial noise variance gives a reasonable signal to noise ratio.
        # Fix to that value for a few iterations to avoid early local minima
        for i in range(len(self.M.layers)):
            output_var = self.M.layers[i].Y.var() if i==0 else self.M.layers[i].Y.mean.var()
            self.M.layers[i].Gaussian_noise.variance = output_var*0.01
            self.M.layers[i].Gaussian_noise.variance.fix()

        self.M.optimize_restarts(num_restarts = num_restarts, robust = True, verbose = self.verbose, parallel = (num_processes is not None), num_processes = num_processes, messages = "True", optimizer = 'lbfgs', start = None, max_iters = max_iters, ipython_notebook = False, clear_after_finish = True)

        # Unfix noise variance now that we have initialized the model
        for i in range(len(self.M.layers)):
            self.M.layers[i].Gaussian_noise.variance.unfix()

        self.M.optimize_restarts(num_restarts = num_restarts, robust = True, verbose = self.verbose, parallel = (num_processes is not None), num_processes = num_processes, messages = "True", optimizer = 'lbfgs', start = None, max_iters = max_iters, ipython_notebook = False, clear_after_finish = True)

    def update(self, newdata : Data, do_train: bool = False, **kwargs):

        #XXX TODO
        self.train(newdata, **kwargs)

    def predict(self, points : Collection[np.ndarray], tid : int, **kwargs) -> Collection[Tuple[float, float]]:

        (mu, var) = self.M.predict(np.concatenate((self.T[tid], x)).reshape((1, self.DT + self.DI)))

        return (mu, var)

		
if __name__ == '__main__':
	def objective(point):
		return point
	mpi_comm = MPI.Comm.Get_parent()
	mpi_rank = mpi_comm.Get_rank()
	mpi_size = mpi_comm.Get_size()
	(modeler, data, restart_iters, kwargs) = mpi_comm.bcast(None, root=0)
	restart_iters_loc = restart_iters[mpi_rank:len(restart_iters):mpi_size]
	tmpdata = modeler.train_mpi(data, i_am_manager = False, restart_iters = restart_iters_loc, **kwargs)
	res = mpi_comm.gather(tmpdata, root=0) 
	mpi_comm.Disconnect()			
		