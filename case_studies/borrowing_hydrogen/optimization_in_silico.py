from surrogate_model_functions import loo_error
from summit.strategies import TSEMO
from summit.models import GPyModel
from summit.data import solvent_ds, ucb_ds, DataSet
from summit.domain import Domain, DescriptorsVariable,ContinuousVariable
from summit.initial_design import LatinDesigner

import GPy
from sklearn.decomposition import PCA
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

import logging
from datetime import datetime as dt

#Constants
RANDOM_SEED = 1000
NUM_BATCHES = 4
BATCH_SIZE = 8
NUM_COMPONENTS = 3


#Random state
random_state = np.random.RandomState(RANDOM_SEED)

# #Logging
# logging.basicConfig(filename='log.txt', level=logging.INFO)
# logger = logging.getLogger(__name__)

def create_pcs_ds(num_components):
    '''Create dataset with principal components'''
    #Read in solubility data
    solubilities = pd.read_csv('inputs/solubilities.csv')
    solubilities = solubilities.set_index('cas_number')
    solubilities = DataSet.from_df(solubilities)

    #Merge data sets
    solvent_ds_full = solvent_ds.join(solubilities)
    solvent_ds_final = pd.merge(solvent_ds_full, ucb_ds, left_index=True,right_index=True)
    print(f"{solvent_ds_final.shape[0]} solvents for optimization")

    #Double check that there are no NaNs in the descriptors
    values = solvent_ds_final.data_to_numpy()
    values = values.astype(np.float64)
    check = np.isnan(values)
    assert check.all() == False

    #Transform to principal componets
    pca = PCA(n_components=num_components)
    pca.fit(solvent_ds_full.standardize())
    pcs = pca.fit_transform(solvent_ds_final.standardize())
    explained_var = round(pca.explained_variance_ratio_.sum()*100)
    expl = f"{explained_var}% of variance is explained by {num_components} principal components."
    print(expl)

    #Create a new dataset with just the principal components
    metadata_df = solvent_ds_final.loc[:, solvent_ds_final.metadata_columns]
    pc_df = pd.DataFrame(pcs, columns = [f'PC_{i+1}' for i in range(num_components)], 
                        index=metadata_df.index)
    pc_ds = DataSet.from_df(pc_df)
    return pd.concat([metadata_df, pc_ds], axis=1)

# Set up test problem
AD1 = 8.5
AD2 = 0.7
EAD1 = 50
EAD2 = 70
R = 8.314
cd1 = lambda t, T, Es: AD1*t*np.exp(-(EAD1+Es)/T)
cd2 = lambda t, T, Es: AD2*t*np.exp(-(EAD2+Es)/T)
Es1 = lambda pc1, pc2, pc3: -20*pc2*abs(pc3) + 0.025*pc1**3
Es2 = lambda pc1, pc2, pc3: 15*pc2*pc3-40*pc3**2

def experiment(solvent_cas, solvent_ds, random_state=np.random.RandomState()):
    pc_solvent = solvent_ds.loc[solvent_cas][solvent_ds.data_columns].to_numpy()
    es1 = Es1(pc_solvent[0], pc_solvent[1], pc_solvent[2])
    es2 = Es2(pc_solvent[0], pc_solvent[1], pc_solvent[2])
    T = 5 * random_state.randn(1) + 393
    t = 0.1 * random_state.randn(1) + 7
    exper_cd1 = cd1(t, T, es1)
    exper_cd2 = cd2(t, T, es2)

    #Conversion with noise
    conversion = exper_cd1 + exper_cd2
    max_conversion = 95.0 + random_state.randn(1)*2
    conversion = min(max_conversion[0], conversion[0])
    conversion = conversion + random_state.randn(1)*2
    conversion=conversion[0]
                     
    de = abs(exper_cd1-exper_cd2)/(exper_cd1 +exper_cd2)
    max_de =  0.98 + random_state.randn(1)*0.02
    de = min(max_de[0], de[0])
    de = de + random_state.randn(1)*0.02
    de = de[0]
    return np.array([conversion, de*100])

#Create up optimization domain
def create_domain(solvent_ds):
    domain = Domain()
    domain += DescriptorsVariable(name='solvent',
                                description='solvent for the borrowing hydrogen reaction',
                                ds=solvent_ds)
    domain += ContinuousVariable(name='conversion',
                                description='relative conversion to triphenylphosphine oxide determined by LCMS',
                                bounds=[0, 100],
                                is_output=True)
    domain += ContinuousVariable(name='de',
                                description='diastereomeric excess determined by ratio of LCMS peaks',
                                bounds=[0, 100],
                                is_output=True)
    return domain

def optimization_setup(domain):
    input_dim = domain.num_continuous_dimensions()+domain.num_discrete_variables()
    kernels = [GPy.kern.Matern52(input_dim = input_dim, ARD=True)
            for _ in range(2)]
    models = [GPyModel(kernel=kernels[i]) for i in range(2)]
    return TSEMO(domain, models)

def generate_initial_experiment_data(domain, solvent_ds, batch_size):
    #Initial design
    lhs = LatinDesigner(domain,random_state)
    initial_design = lhs.generate_experiments(batch_size)

    #Initial experiments
    initial_experiments = [experiment(cas, solvent_ds, random_state) 
                           for cas in initial_design.to_frame()['cas_number']]
    initial_experiments = pd.DataFrame(initial_experiments, columns=['conversion', 'de'])
    initial_experiments = DataSet.from_df(initial_experiments)
    design_df = initial_design.to_frame()
    design_df = design_df.rename(index=int, columns={'cas_number': 'solvent'})
    design_ds = DataSet.from_df(design_df)
    return initial_experiments.merge(design_ds, left_index=True, right_index=True)

def run_optimization(tsemo, initial_experiments,solvent_ds,
                     batch_size, num_batches,
                     num_components, normalize=False):
    #Create storage arrays
    lengthscales = np.zeros([num_batches, num_components, 2])
    log_likelihoods = np.zeros([num_batches, 2])
    loo_errors = np.zeros([num_batches, 2])
    previous_experiments = initial_experiments

    #Run the optimization
    for i in range(num_batches):
        #Generate batch of solvents
        design = tsemo.generate_experiments(previous_experiments, batch_size, 
                                            normalize=normalize)

        #Calculate model parameters for further analysis later
        lengthscales[i, :, :] = np.array([model._model.kern.lengthscale.values for model in tsemo.models]).T
        log_likelihoods[i, :] = np.array([model._model.log_likelihood() for model in tsemo.models]).T
        for j in range(2):
            loo_errors[i, j] = loo_error(tsemo.x, np.atleast_2d(tsemo.y[:, j]).T)

        #Run the "experiments"                                    
        new_experiments = [experiment(cas, solvent_ds, random_state)
                           for cas in design.index.values]
        new_experiments = np.array(new_experiments)

        #Combine new experimental data with old data
        new_experiments = DataSet({('conversion', 'DATA'): new_experiments[:, 0],
                                   ('de', 'DATA'): new_experiments[:, 1],
                                   ('solvent', 'DATA'): design.index.values})
        new_experiments = new_experiments.set_index(np.arange(batch_size*(i+1), batch_size*(i+2)))
        new_experiments.columns.names = ['NAME', 'TYPE']
        previous_experiments = previous_experiments.append(new_experiments)

    return lengthscales,log_likelihoods, loo_errors, 



if __name__ == '__main__':
    solvent_pcs_ds = create_pcs_ds(num_components=NUM_COMPONENTS)
    domain = create_domain(solvent_pcs_ds)
    tsemo = optimization_setup(domain)
    initial_experiments = generate_initial_experiment_data(domain,
                                                           solvent_pcs_ds,
                                                           BATCH_SIZE)
    lengthscales, log_likelihoods, loo_errors = run_optimization(tsemo, initial_experiments, 
                                                                 solvent_pcs_ds,
                                                                 num_batches=NUM_BATCHES,
                                                                 batch_size=BATCH_SIZE, 
                                                                 num_components=NUM_COMPONENTS)
    # Write parameters to disk
    np.save('outputs/in_silico_lengthscales', lengthscales)
    np.save('outputs/in_silico_log_likelihoods', log_likelihoods)
    np.save('outputs/in_silico_loo_errors', loo_errors)
    with open('outputs/in_silico_metadata.txt',  'w') as f:
        f.write(f"Random seed: {RANDOM_SEED}\n")
        f.write(f"Number of principal components: {NUM_COMPONENTS}\n")
        f.write(f"Number of batches: {NUM_BATCHES}\n")
        f.write(f"Batch size: {BATCH_SIZE}\n")


