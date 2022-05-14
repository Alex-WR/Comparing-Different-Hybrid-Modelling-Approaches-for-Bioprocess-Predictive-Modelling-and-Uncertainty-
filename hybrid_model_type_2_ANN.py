#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jun 24 11:36:29 2021

@author: alexanderRogers
"""

import numpy as np                         
import torch
from scipy.optimize import fmin
from scipy.integrate import odeint
from copy import deepcopy   

# %% <Import Cache>
import HYBRID_MODEL_MAIN as MAIN
rnd = np.random.RandomState(MAIN.rnd_seed)
torch.manual_seed(MAIN.rnd_seed) 
kinetic_parameters = torch.load(MAIN.cache + '\_kinetic_parameters')
data_key = torch.load(MAIN.cache + '\_data_key')
data = torch.load(MAIN.cache + '\_insilico_data')


# %% <Load and Organise Data>

data = torch.load(MAIN.cache + '\_insilico_data')
data = {e: x.reshape((-1, 4, 1)) for e, x in data.items()}

# Define Input (X), Intermediate (U) and Output (Y) Variable Indexes:
I = {'T': 0, 'CX': 1, 'CS': 2, 'CP': 3, 'eX': 4, 'eS': 5, 'eP': 6}
x_index = [I['CX'], I['CS'], I['CP']]
u_index = [I['eX'], I['eS'], I['eP']]
y_index = [I['CX'], I['CS'], I['CP']]


# %% <Define Kinetic Model and Hybrid Model>

from model_utilities import ANN_trainer

class HybridModel(ANN_trainer):
    def __init__(self, params, hyprams, x_index, u_index, y_index):
        super().__init__(hyprams, x_index, u_index, y_index)
        
        self.mu_m  = params['mu_m']
        self.k_c   = params['k_c']
        self.y_sx  = params['y_sx']
        self.beta  = params['beta']
        
    def multi_step_ahead(self, data, sample_size=500, smooth_edge_ratio=10):
        data = {k: torch.from_numpy(x).type(self.dtype) for k, x in data.items()}
        data_pred = {}
        torch.manual_seed(1)
        
        with torch.no_grad():
            for k, y in data.items():
                y = torch.dstack([deepcopy(y)] * (sample_size + 1))
                for i in range(y.shape[0] - 1):
                    
                    for j in range(sample_size + 1):
                        mean, std = self.predict(standardize(y[i, self.x_index, j], self.x_index))
                        if j != 0: mean = torch.normal(mean, std)
                        y[i, self.u_index, j] = inv_standardize(mean, self.u_index)
                        y[i:i+2, :, j] = torch.tensor(self._prior_(y[i:i+2, :, j].numpy()), dtype=self.dtype)
                        y[i+1, I['CS'], j] = max(0, y[i+1, I['CS'], j])
                        
                
                n = int(sample_size / smooth_edge_ratio)
                y_mea = y[:, :, 0]
                y_rnk, _ = torch.sort(y[:, self.y_index, 1:], dim=2)
                y_min = torch.mean(y_rnk[:, :, -n:], dim=2)
                y_max = torch.mean(y_rnk[:, :, :n], dim=2)
                
                y = torch.column_stack([y_mea, y_min, y_max])
                y[:, self.y_index][y[:, self.y_index] < 0] = 0
                data_pred[k] = np.array(y)
        return data_pred

    def _prior_(self, y):
        error_X, error_S, error_P = y[0, I['eX']], y[0, I['eS']], y[0, I['eP']]
        dXdt = lambda Y, t: error_X + MAIN.KN_dXdt(*Y, self.mu_m, self.k_c)
        dSdt = lambda Y, t: min(0, error_S + MAIN.KN_dSdt(*Y, self.mu_m, self.k_c, self.y_sx))
        dPdt = lambda Y, t: error_P + MAIN.KN_dPdt(*Y, self.beta)
        dYdt = lambda Y, t: np.array([dXdt(Y, t), dSdt(Y, t), dPdt(Y, t)])
        y[:, self.y_index] = odeint(dYdt, y[0, self.y_index], t=y[:, I['T']])
        return y
    
    def _obj_func_(self, u, y):
        y[:, self.u_index] = u
        y_pred = self._prior_(deepcopy(y))
        mse = sum(((y[1, self.y_index] - y_pred[1, self.y_index]) / y[1, self.y_index]) ** 2)
        return mse

    def estimate_errors(self, data):
        new_data = {}
        for k, y in data.items():
            y = np.column_stack([y, np.zeros((y.shape[0], len(self.y_index), 1))])
            u_opt = [0, 0, 0]
            for i in range(y.shape[0] - 1):
                u_opt = fmin(self._obj_func_, x0=u_opt, args=(y[i:i+2, :, 0],), disp=False)
                y[i, self.u_index, 0] = u_opt
            new_data[k] = y
        return new_data

class KineticModel(object):
    def __init__(self, params, x_index, y_index):
        self.y_index = y_index
        self.x_index = x_index
        
        # Unpack Kinetic Model Parameters
        self.mu_m  = params['mu_m']
        self.k_c   = params['k_c']
        self.y_sx  = params['y_sx']
        self.beta  = params['beta']
        
    def multi_step_ahead(self, data, smooth=False):
        data_pred = {}
        for k, y in data.items():
            if smooth: n = 100 
            else: n = y.shape[0]
            y1 = np.zeros((n, len(I), 1))
            dXdt = lambda Y, t: MAIN.KN_dXdt(*Y, self.mu_m, self.k_c)
            dSdt = lambda Y, t: MAIN.KN_dSdt(*Y, self.mu_m, self.k_c, self.y_sx)
            dPdt = lambda Y, t: MAIN.KN_dPdt(*Y, self.beta)
            dYdt = lambda Y, t: np.array([dXdt(Y, t), dSdt(Y, t), dPdt(Y, t)])
            y1[:, I['T'], 0] = np.linspace(y[0, I['T'], 0], y[-1, I['T'], 0], n).reshape(-1)
            y1[:, self.y_index, 0] = odeint(dYdt, y[0, self.y_index, 0].reshape(-1), t=y1[:, I['T'], 0])
            data_pred[k] = deepcopy(y1)
        return data_pred
    
    
# %% <Initialize Model and Hybrid Model>   

# Define Hyperparameters:
hyparams = {'hidden_size'   : [6],
            'learning_rate' :  0.05,
            'epochs'        :  3000}

# Define Kinetic Model Parameters:
hyb_model = HybridModel(kinetic_parameters, hyparams, x_index, u_index, y_index)
data = hyb_model.estimate_errors(data)
kin_model = KineticModel(kinetic_parameters, x_index, y_index)


# %% <Preprocess Data>

# Capture Stats:
stacked_data = np.row_stack([x for x in data.values()])
mean = np.mean(stacked_data, 0, keepdims=True)
stdv = np.std(stacked_data, 0, keepdims=True)

# Augment:
n = 20; noise = 0.03; var_to_aug = ['CX', 'CS', 'CP']
aug_map = np.reshape([1 if k in var_to_aug else 0 for k in I.keys()], (1, -1, 1))
f = lambda x: rnd.normal(size=(*x.shape[:-1], n)) * aug_map
g = lambda k, x: np.dstack((x, x * (1 + f(x) * noise)))
aug_data = {k: g(k, x) for k, x in data.items()}

# Standardize:
def standardize(data, s=slice(None)):
    if type(data) == dict: return {k: (x - mean) / stdv for k, x in data.items()}
    else: return ((data - mean[:, s, 0]) / stdv[:, s, 0]).float()
def inv_standardize(data, s=slice(None)):
    if type(data) == dict: return {k: x * stdv + mean for k, x in data.items()}
    else: return (data * stdv[:, s, 0] + mean[:, s, 0]).float()

stan_data = standardize(aug_data)


# %% <Train and Test Kinetic and Hybrid Model>

# Training and Validation Split:
train_split = MAIN.train_test_split['train']
test_split  = MAIN.train_test_split['test'] 

data_train = {x: stan_data[x] for x in train_split}
data_test  = {x: data[x] for x in test_split}
data_train_inv = {x: data[x] for x in train_split}

# Fit Hybrid Model;
hyb_model.fit_aggregate(data_train)

# Predict Hybrid Model Multi-Step-Ahead Process Trajectory:
hyb_pred_test = hyb_model.multi_step_ahead(data_test)
hyb_pred_train = deepcopy(hyb_model).multi_step_ahead(data_train_inv)

# Predict Kinetic Model Multi-Step-Ahead Process Trajectory:
kin_pred_test = kin_model.multi_step_ahead(data_test, smooth=True)
    
safe = deepcopy(hyb_pred_test)

# %% <Plotting Predictions and Learning Curves>      

hyb_pred_test = deepcopy(safe)

import matplotlib.pyplot as plt
import matplotlib as mpl
size = 20

# Plot Multi-Step-Ahead Process Trajectorys:
varbs  = ['CX', 'CS', 'CP']
labels = ['Biomass Concentration (g/L)', 'Glucose Concentration (g/L)', 'Astaxanthin Concentration (mg/L)']

ms  = size / 1.7
lw  = size / 6
mpl.rc('font', **{'size'   : size * 1.2})
mpl.rcParams['xtick.major.size']  = size / 3
mpl.rcParams['xtick.major.width'] = size / 8
mpl.rcParams['ytick.major.size']  = size / 3
mpl.rcParams['ytick.major.width'] = size / 8
mpl.rcParams['axes.linewidth']    = size / 10
mpl.rcParams['figure.constrained_layout.use'] = True

exp_to_plot = [4, 6]
mark_colours = ['#a60000', '#193f61']
line_colours = ['#d10000', '#1F4E79']
face_colours = ['#fc0303', '#2E75B6']
fig = plt.figure(figsize=(12 * size / 10, 4 * size / 10))
j = 0
for (k, exp), hyb, kin in zip(data_test.items(), hyb_pred_test.values(), kin_pred_test.values()):
    if k in exp_to_plot:
        for i, (var, label) in enumerate(zip(varbs, labels)):
            x_exp = exp[:, I['T']]
            y_exp = exp[:, I[var]]
            x_hyb = hyb[:, I['T']]
            l_hyb = hyb[:, I[var] + 6]
            u_hyb = hyb[:, I[var] + 9]
            y_hyb = hyb[:, I[var]]
            x_kin = kin[:, I['T']]
            y_kin = kin[:, I[var]]
            plt.subplot(1, 3, i+1)
            hybrid, = plt.plot(x_hyb, y_hyb, '--', color=line_colours[j], label='Hybrid Model', ms=ms, lw=lw)
            plt.fill_between(x_hyb, l_hyb, u_hyb, facecolor=face_colours[j], alpha=0.2)
            experimental, = plt.plot(x_exp, y_exp, 'o', color=mark_colours[j], label='Experiment', ms=ms, lw=lw)
            #plt.suptitle('Experiment: {}'.format(k))
            TB1, = plt.plot([np.nan], [np.nan], '--', color=face_colours[0], label='Test Batch 1', ms=ms, lw=lw)
            TB2, = plt.plot([np.nan], [np.nan], '--', color=face_colours[1], label='Test Batch 3', ms=ms, lw=lw)
            TDS, = plt.plot([np.nan], [np.nan], 'o', color='#292828', label='Experiment', ms=ms, lw=lw)
            if var is 'CX': loc = 'upper left'; 
            else: loc = 'best'
            plt.legend(handles=[TB1, TB2], loc=loc)
            plt.xlabel('Time (h)')
            plt.ylabel(label)
    if k in exp_to_plot: j+=1
plt.show()

exp_to_plot = [5, 7]
mark_colours = ['#193f61', '#a60000']
line_colours = ['#1F4E79', '#d10000']
face_colours = ['#2E75B6', '#fc0303']
fig = plt.figure(figsize=(12 * size / 10, 4 * size / 10))
j = 0
for (k, exp), hyb, kin in zip(data_test.items(), hyb_pred_test.values(), kin_pred_test.values()):
    if k in exp_to_plot:
        for i, (var, label) in enumerate(zip(varbs, labels)):
            x_exp = exp[:, I['T']]
            y_exp = exp[:, I[var]]
            x_hyb = hyb[:, I['T']]
            l_hyb = hyb[:, I[var] + 6]
            u_hyb = hyb[:, I[var] + 9]
            y_hyb = hyb[:, I[var]]
            x_kin = kin[:, I['T']]
            y_kin = kin[:, I[var]]
            plt.subplot(1, 3, i+1)
            hybrid, = plt.plot(x_hyb, y_hyb, '--', color=line_colours[j], label='Hybrid Model', ms=ms, lw=lw)
            plt.fill_between(x_hyb, l_hyb, u_hyb, facecolor=face_colours[j], alpha=0.2)
            experimental, = plt.plot(x_exp, y_exp, 'o', color=mark_colours[j], label='Experiment', ms=ms, lw=lw)
            #plt.suptitle('Experiment: {}'.format(k))
            TB2, = plt.plot([np.nan], [np.nan], '--', color=face_colours[1], label='Test Batch 2', ms=ms, lw=lw)
            TB4, = plt.plot([np.nan], [np.nan], '--', color=face_colours[0], label='Test Batch 4', ms=ms, lw=lw)
            TDS, = plt.plot([np.nan], [np.nan], 'o', color='#292828', label='Experiment', ms=ms, lw=lw)
            if var is 'CX': loc = 'upper left'; 
            else: loc = 'best'
            plt.legend(handles=[TB2, TB4], loc=loc)
            plt.xlabel('Time (h)')
            plt.ylabel(label)
    if k in exp_to_plot: j+=1
plt.show()

# =============================================================================
# for (k, exp), hyb, kin in zip(data_test.items(), hyb_pred_test.values(), kin_pred_test.values()):
#     fig = plt.figure(figsize=(12 * size / 10, 4 * size / 10))
#     for i, (var, label) in enumerate(zip(varbs, labels)):
#         x_exp = exp[:, I['T']]
#         y_exp = exp[:, I[var]]
#         x_hyb = hyb[:, I['T']]
#         l_hyb = hyb[:, I[var] + 6]
#         u_hyb = hyb[:, I[var] + 9]
#         y_hyb = hyb[:, I[var]]
#         x_kin = kin[:, I['T']]
#         y_kin = kin[:, I[var]]
#         plt.subplot(1, 3, i+1)
#         experimental, = plt.plot(x_exp, y_exp, 'o', color='k', label='Experiment', ms=ms, lw=lw)
#         hybrid, = plt.plot(x_hyb, y_hyb, '--', color='#1F4E79', label='Hybrid Model', ms=ms, lw=lw)
#         plt.fill_between(x_hyb, l_hyb, u_hyb, facecolor='#2E75B6', alpha=0.2)
#         kinetic, = plt.plot(x_kin, y_kin, '-', color='#eb3434', label='Kinetic Model', ms=ms, lw=lw)
#         #plt.suptitle('Experiment: {}'.format(k))
#         plt.xlabel('Time (h)')
#         plt.ylabel(label)
#         plt.legend(handles=[experimental, hybrid, kinetic])
#     plt.show()
# =============================================================================

# Estimate MRPE and MRSD:
i0 = 2;
for exps in [[4], [6], [5]]:
    ex = data_test; md = hyb_pred_test
    MRPE = {i: np.mean(np.absolute((ex[i][i0:, 1:4, 0] - md[i][i0:, 1:4])), axis=0) for i in exps}
    MRPE_X = np.mean([MRPE[i][0] for i in exps]) / 3
    MRPE_S = np.mean([MRPE[i][1] for i in exps]) / 5
    MRPE_P = np.mean([MRPE[i][2] for i in exps]) / 10
    
    MRSD = {i: np.mean((1/4) * np.absolute((md[i][i0:, 10:13] - md[i][i0:, 7:10])), axis=0) for i in exps}
    MRSD_X = np.mean([MRSD[i][0] for i in exps]) / 3
    MRSD_S = np.mean([MRSD[i][1] for i in exps]) / 5
    MRSD_P = np.mean([MRSD[i][2] for i in exps]) / 10
    
    print('===============================')
    print(exps)
    print('===============================')
    print('(X)  MRPE: {:.3f},     MRSD: {:.3f}'.format(MRPE_X,  MRSD_X))
    print('(S)  MRPE: {:.2f},     MRSD: {:.3f}'.format(MRPE_S,  MRSD_S))
    print('(P)  MRPE: {:.2f},     MRSD: {:.3f}'.format(MRPE_P,  MRSD_P))
    print('===============================')
    print('===============================')

ex = data_test; md = kin_model.multi_step_ahead(data_test, smooth=False)
MRPE = {i: 200 * np.mean(np.absolute((ex[i][i0:, 1:4, 0] - md[i][i0:, 1:4, 0]) / (ex[i][i0:, 1:4, 0] + md[i][i0:, 1:4, 0])), axis=0) for i in exps}
MRPE_X = np.mean([MRPE[i][0] for i in exps])
MRPE_S = np.mean([MRPE[i][1] for i in exps])
MRPE_P = np.mean([MRPE[i][2] for i in exps])

print('=================')
print('=================')
print('(X)  MRPE: {:.2f}'.format(MRPE_X))
print('(S)  MRPE: {:.1f}'.format(MRPE_S))
print('(P)  MRPE: {:.2f}'.format(MRPE_P))
print('=================')
print('=================')

exps = [0, 1, 2, 3]
ex = {x: data[x] for x in train_split}; md = kin_model.multi_step_ahead(ex, smooth=False)
MRPE = {i: 200 * np.mean(np.absolute((ex[i][i0:, 1:4, 0] - md[i][i0:, 1:4, 0]) / (ex[i][i0:, 1:4, 0] + md[i][i0:, 1:4, 0])), axis=0) for i in exps}
MRPE_X = np.mean([MRPE[i][0] for i in exps])
MRPE_S = np.mean([MRPE[i][1] for i in exps])
MRPE_P = np.mean([MRPE[i][2] for i in exps])

print('=================')
print('=================')
print('(X)  MRPE: {:.2f}'.format(MRPE_X))
print('(S)  MRPE: {:.1f}'.format(MRPE_S))
print('(P)  MRPE: {:.2f}'.format(MRPE_P))
print('=================')
print('=================')

# Plot Learning Curve:
epochs          = hyb_model.training_history[:, 0]
training_loss   = hyb_model.training_history[:, 1]
validation_loss = hyb_model.training_history[:, 2]
fig = plt.figure(figsize=(8, 6))
training, = plt.plot(epochs, training_loss, 'b-', label='Training')
validation, = plt.plot(epochs, validation_loss, 'r-', label='Validation')
plt.suptitle('Learning Curve')
plt.xlabel('Epochs')
plt.ylabel('MSE Loss')
plt.legend(handles=[training, validation])
plt.show()

# Plot Discrepancy Parity:
varbs = ['eX', 'eS', 'eP']
labels = [r'$\epsilon_X$', r'$\epsilon_S$', r'$\epsilon_P$']
fig = plt.figure(figsize=(12 * size / 10, 4 * size / 10))
for i, var in enumerate(varbs):
    for exp, hyb, in zip(data_train_inv.values(), hyb_pred_train.values()):
        plt.subplot(1, 3, i+1)
        training, = plt.plot(exp[:, I[var], 0], hyb[:, I[var]], 'X', color='#eb3434', label='Train Data', ms=ms*0.8, lw=lw)
    for exp, hyb, in zip(data_test.values(), hyb_pred_test.values()):
        identity, = plt.plot(exp[:, I[var]], exp[:, I[var]], 'k-', label='Identity', ms=ms*0.8, lw=lw)
        test, = plt.plot(exp[:, I[var]], hyb[:, I[var]], 'o', color='#2E75B6', label='Test Data', ms=ms*0.8, lw=lw)
    plt.xlabel('Expected: ' + labels[i])
    plt.ylabel('Predicted: ' + labels[i])
    plt.legend(handles=[identity, training, test])
plt.show()

from scipy import signal

# Plot Hybrid Parameter WRT:
varbs = ['eX', 'eS', 'eP']
labels = [r'$\epsilon_X$', r'$\epsilon_S$', r'$\epsilon_P$']
#colours = ['#545454', '#8a8a8a', 'k', 'b']
colours = ['#E06669', '#92D050', '#2E75B6', 'k']
fig = plt.figure(figsize=(12 * size / 10, 4 * size / 10))
for i, var in enumerate(varbs):
    handles = []
    for exp, (k, hyb) in zip(data_train_inv.values(), data_train_inv.items()):
        if k != 3:
            if k == 3:
                n = 10
                x = signal.savgol_filter(np.ravel(hyb[-n:, I[var]]), window_length=5, polyorder=2)
                hyb[-n:, I[var]] = x.reshape([-1, 1])
            plt.subplot(1, 3, i+1)
            predicted, = plt.plot(hyb[:, I['T']], hyb[:, I[var]], 'o-',  color=colours[k], label='Batch: ' + str(k+1), ms=ms*0.8, lw=lw)
            handles.append(predicted)
            plt.xlabel('Time (h)')
            plt.ylabel('Predicted: ' + labels[i])
    plt.legend(handles=handles)
plt.show()

# %% <Export Cache>

