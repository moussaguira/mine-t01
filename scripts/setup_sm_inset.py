import datetime as dt
import glob
import os
from pathlib import Path
import shutil
import numpy as np
import pandas as pd
import flopy.mf6 as mf6
from gisutils import get_values_at_points
from mfobs.modflow import get_perioddata
from mfsetup import MF6model, load_modelgrid
from mfsetup.grid import get_ij
from mfsetup.utils import get_input_arguments
from datetime import datetime as dtt



def setup_grid(cfg_file):
    """Just set up (a shapefile of) the model grid. 
    For trying different grid configurations."""
    m = MF6model(cfg=cfg_file)
    m.setup_grid()


def parse_wb_ascii_filename(filename):
    """Get the crop type and month for each 
    Arc ascii grid of crop types produced by the water balance team.
    """
    filename = Path(filename)
    _, _, year, _ = filename.name.split('__')
    year = int(year)
    year = pd.Timestamp(month=1, year=year, day=1).year
    return year


def add_crop_type_to_wells(cdl_grid_rasters, model_name, model_ws):
    """Sample Cropland Data Layers (CDLs), and assign a crop type to each wel package flux
    * by year for 2007 on,
    * for pre-2007 (multi-year) spin-up periods, assign as aquaculture or other,
    based on total extent of aquaculture cells in CDL dataset
    
    Note
    ----
    After running this, may want to add auxillary entry to options block of WEL input file:
    e.g.:
    BEGIN options
    AUXILIARY datasource
    BOUNDNAMES
    PRINT_INPUT
    PRINT_FLOWS
    SAVE_FLOWS
    AUTO_FLOW_REDUCE       0.10000000
    END options
    """
    model_ws = Path(model_ws)
    wel_files = sorted(glob.glob(str(model_ws / 'external/wel_*.dat')))
    grid = load_modelgrid(model_ws / f'{model_name}_grid.json')
    perioddata = pd.read_csv(model_ws / 'tables/stress_period_data.csv')
    perioddata['start_datetime'] = pd.to_datetime(perioddata['start_datetime'])

    cdl_grids = {}
    is_aquaculture_3d = []
    for cdl_raster in cdl_grid_rasters:
    
        year = parse_wb_ascii_filename(cdl_raster)
        grid_croptypes = get_values_at_points(cdl_raster, 
                                            x=grid.xcellcenters, y=grid.ycellcenters,
                                            method='nearest'
                                            )
        cdl_grids[year] = grid_croptypes
        # 111 == open water
        # 92 == aquaculture (in the CDL)
        is_aquaculture = (grid_croptypes == 111) | (grid_croptypes == 92)
        is_aquaculture_3d.append(is_aquaculture)
    is_aquaculture_3d = np.array(is_aquaculture_3d)
    is_aquaculture = is_aquaculture_3d.any(axis=0)
    
    # map crop types from CDL numbers to words
    # (unlisted crop types go to 'other')
    crop_types = {1: 'corn',
                  2: 'cotton',
                  3: 'rice',
                  5: 'soybeans',
                  26: 'soybeans',
                  92: 'aquaculture',
                  111: 'aquaculture'
                  }
    
    # map stress periods to years
    years = dict(zip(perioddata.per, 
                     [ts.year for ts in perioddata['start_datetime']]))

    for wel_file in wel_files:

        wel_file = Path(wel_file)
        per = int(wel_file.stem.split('_')[1])  # zero-based stress period

        # read the existing list-type external file
        df = pd.read_csv(wel_file, delim_whitespace=True)
        
        if 'datasource' not in df.columns:
            df['datasource'] = df['boundname']
        i = df['i'].values -1
        j = df['j'].values -1
        # only use 2 crop types for spin-up periods
        # (aquaculture or not)
        if per < 7:
            crop_type = ['aquaculture' if item else 'other' for item in is_aquaculture[i, j]]
            df['boundname'] = crop_type
        else:
            period_cdl_grid = cdl_grids[years[per]]
            crop_type = [crop_types.get(value, 'other') for value in period_cdl_grid[i, j]]
            df['boundname'] = crop_type
            datasources = np.array([s.split('_')[0] for s in df['datasource']])
            df.loc[datasources != 'iwum', 'boundname'] = 'non-ag'
        df.to_csv(wel_file, index=False, sep=' ')

   
def add_datasource_col_to_wells(model_ws):
    """Parse existing "datasource" column from well files; 
    rename existing datasource column to datasource_id; 
    make new datasource column with just data source (i.e. "iwum" or "meras2").
    This will allow for coarse parameters that can adjust all values by data source.
    """
    model_ws = Path(model_ws)
    wel_files = sorted(glob.glob(str(model_ws / 'external/wel_*.dat')))

    for wel_file in wel_files:
        wel_file = Path(wel_file)
        # read the existing list-type external file
        df = pd.read_csv(wel_file, delim_whitespace=True)
        cols = ['#k', 'i', 'j', 'q', 
                'boundname', 'datasource_id', 'datasource']
        if len(df.columns) == 5 and df.columns[-1] == 'boundname':
            cols = cols[:5]
            df['datasource'] = [s.split('_')[0] for s in df['boundname']]
        else:
            if 'datasource' not in df.columns:
                raise ValueError("No 'datasource' column in WEL package files. "
                                "Make sure to run add_crop_type_to_wells() "
                                "prior to running this function.")
            if 'datasource_id' not in df.columns:
                df['datasource_id'] = df['datasource']
            df['datasource'] = [s.split('_')[0] for s in df['datasource_id']]
            df = df[cols]
        df.to_csv(wel_file, index=False, sep=' ')
        
        
def setup_model(cfg_file):
    m = MF6model.setup_from_yaml(cfg_file)
    m.write_input()
    

def just_redo_the_head_obs(obs_file, headobs_info_file, 
                           idomain_files, model_grid_json):
    """Option to just update the head observations from the preprocessed data,
    without having to rebuild the whole model.
    """
    grid = load_modelgrid(model_grid_json)
    head_obs_info = pd.read_csv(headobs_info_file)
    idomain_files = sorted(list(idomain_files))

    # hard-code number of layers
    idomain = np.empty((21, grid.nrow, grid.ncol), dtype=int)
    x, y = head_obs_info[['x', 'y']].T.values
    i, j = get_ij(grid, x, y)
    head_obs_info['i'] = i
    head_obs_info['j'] = j
    for i in range(len(idomain_files)):
        idomain[i,:,:] = np.loadtxt(idomain_files[i])
        
    new_obs_file = Path(obs_file).with_suffix('.2.obs')
    with open(obs_file) as src:
        with open(new_obs_file, 'w') as dest:
            for line in src:
                dest.write(line)
                if '# via modflow-setup version' in line:
                    date = dt.datetime.now().strftime('%Y-%m-%d')
                    dest.write(f'# edited by setup_delta_inset.py {date}\n')
                if 'BEGIN continuous  FILEOUT' in line:
                    fname = line.strip().split()[-1]
                    break
            for i, j, site_no in zip(head_obs_info['i'], 
                                  head_obs_info['j'], 
                                  head_obs_info['site_no']):
                for k in range(21):
                    idm = idomain[k, i, j]
                    if idm > 0:
                        dest.write(f'  {site_no.lower()} HEAD  {k+1} {i+1} {j+1}\n')
            dest.write(f'END continuous  FILEOUT  {fname}\n')    
    

def fix_sfr_observations(sfr_obsfile, reach_data):
    """Kludge to fix some SFR locations that were specified 
    incorrectly by Modflow-setup.
    """
    # site number: COMID;
    # COMID will then be translated to RNO using packagedata
    fixes = {'07288939': 17938222}
    rd = pd.read_csv(reach_data)
    rno = dict(zip(rd['line_id'], rd['rno']))
    
    print('Fixing SFR observations...')
    sfr_obsfile = Path(sfr_obsfile)
    sfr_obsfile_original = sfr_obsfile.with_suffix('.obs.original')
    if not sfr_obsfile_original.exists():
        shutil.copy(sfr_obsfile, sfr_obsfile_original)
    with open(sfr_obsfile_original) as src:
        with open(sfr_obsfile, 'w') as dest:
            for line in src:
                for site_no, comid in fixes.items():
                    if site_no in line:
                        items = line.strip().split()
                        assert len(items) == 3, f"unexpected line length:\n{line}"
                        new_rno = rno[comid]
                        print(f"  {items[0]}: moving from reach {items[2]} to {new_rno}")
                        items[2] = f"{new_rno:.0f}"
                        line = "  " + "  ".join(items) + '\n'
                dest.write(line)    
    
    
def fix_sfr_inflows_runoff(sfrfile, fill_runoff=True):
    """Kludge to fix runoff and inflow specification in SFR package
    
    * Remove runoff from initial steady-state period and period 7 (1998-2007).
      Currently (12/2021) Modflow-setup is hard-coded to specify runoff in 
      any periods with overlapping data, and also apply average values to any
      initial steady-state periods. Would be better to simply neglect runoff
      for steady-state and multi-year periods since it's not clear how representative
      average values are for gw/sw interactions (in terms of the stages they produce).
    * Add average inflow values to multi-year stress periods where they are missing.
      We want this because without it, flows from upstream aren't being included
      (they are exiting at the outlets upstream of the inflow points). 
      Assuming ET losses are negligible, the flood control reservoirs don't change 
      the overall quantity of flow, they just shift it in time from the winter/spring
      to summer/fall.

    Parameters
    ----------
    sfrfile : str or pathlike
        SFR package file
    fill_runoff : bool
        Option to fill multi-year stress periods (2-7) with average runoff
        values from initial steady-state period (1). Only missing runoff
        will be filled (e.g. s.p. 7 overlaps with SWB simulation timeframe,
        so it has runoff values by default). If False, remove runoff from
        initial steady-state and subsequent multi-year periods, as described above.
    """
    sfrfile_copy = sfrfile.with_suffix('.original.sfr')
    shutil.copy(sfrfile, sfrfile_copy)
    with open(sfrfile_copy) as src:
        with open(sfrfile, 'w') as dest:
            inflows = {}
            runoff = {}
            spinup = False
            header = True
            for line in src:
                if header and '#' not in line:
                    date = dt.datetime.now().strftime('%Y-%m-%d')
                    dest.write(f'# modified by setup_delta_inset.py {date}\n')
                    header = False
                if not spinup:
                    dest.write(line)
                # assume period 1 contains average inflow values
                if 'begin period 1' in line.lower():
                    spinup = True
                    for line in src:
                        if 'period 1' in line.lower():
                            break
                        rno, text, value = line.strip().split()
                        if 'inflow' in line:
                            if 1 not in inflows:
                                inflows[1] = {}
                            inflows[1][rno] = line
                        if fill_runoff and 'runoff' in line:
                            if 1 not in runoff:
                                runoff[1] = {}
                            runoff[1][rno] = line
                    continue
                # write stress period blocks for first 7 periods
                # with inflows filled
                # and without runoff
                if 'begin period 8' in line.lower():
                    for per in range(1, 8):
                        if per > 1:
                            dest.write(f'begin period {per}\n')
                        if per not in inflows:
                            inflows[per] = inflows[1]
                        for rno, text in inflows[per].items():
                            dest.write(text)
                        if fill_runoff:
                            if per not in runoff:
                                runoff[per] = runoff[1]
                            for rno, text in runoff[per].items():
                                dest.write(text) 
                        dest.write(f'end period {per}\n\n')
                    dest.write(line)
                    for line in src:
                        dest.write(line)
                if spinup and 'period' in line.lower():
                    per = int(line.strip().split()[-1])
                    if per > 1:
                        for line in src:
                            rno, text, value = line.strip().split()
                            # record the inflow or runoff info if it exists
                            if 'inflow' in line:
                                if per not in inflows:
                                    inflows[per] = {}
                                inflows[per][rno] = line
                            if fill_runoff and 'runoff' in line:
                                if per not in runoff:
                                    runoff[per] = {}
                                runoff[per][rno] = line
                            # add missing inflows or runoff to period
                            if f'period {per}' in line.lower():
                                if per not in inflows:
                                    inflows[per] = inflows[1]
                                else:
                                    for rno, text in inflows[1].items():
                                        if rno not in inflows[per]:
                                            inflows[per][rno] = text
                                if fill_runoff:
                                    if per not in runoff:
                                        runoff[per] = runoff[1]
                                    else:
                                        for rno, text in runoff[1].items():
                                            if rno not in runoff[per]:
                                                runoff[per][rno] = text
                                break
                    continue  
                    
                

            
if __name__ == '__main__':
    start_time = dtt.now()

    # delete original versions of these SFR files from the last model build
    # (otherwise they would be used by adjust_sfr_streambed_tops_set_stages.py to rebuild the SFR package)
    model_ws = Path('../../model/model.calibration/')
    
    rebuild_model = False
    if rebuild_model:
        Path(model_ws / 'external/sm100_packagedata.original.dat').unlink(missing_ok=True)
        Path(model_ws / 'sm100.original.sfr').unlink(missing_ok=True)
        Path(model_ws / 'sm100.sfr.obs.original').unlink(missing_ok=True)
        #setup_grid('../../sm100.yml')
        setup_model('../../sm100.yml')
    
    # post-hoc editing of wel package external files to add crop-type information
    #cdl_grid_rasters = sorted(glob.glob('../source_data/water_use/cdl_grids_1999-2020/*.asc'))
    #add_crop_type_to_wells(cdl_grid_rasters, 'delta500', model_ws)
    add_datasource_col_to_wells(model_ws)

    end_time = dtt.now()
    print('Completion Time was: {}'.format(end_time - start_time))  