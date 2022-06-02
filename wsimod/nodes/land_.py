# -*- coding: utf-8 -*-
"""
Created on Fri May 20 08:58:58 2022

@author: Barney
"""
from wsimod.nodes.nodes import Node, Tank, DecayTank, QueueTank
from wsimod.nodes.nutrient_pool import NutrientPool
from wsimod.core import constants
from math import exp
import sys

class Land_(Node):
    def __init__(self, **kwargs):
        
        
        super().__init__(**kwargs)
        
        surfaces_ = kwargs['surfaces'].copy()
        surfaces = []
        for surface in surfaces_:
            surface['parent'] = self
            surfaces.append(getattr(sys.modules[__name__], surface['type'])(**surface))
            self.mass_balance_ds.append(surfaces[-1].ds)
        
        #Can also do as timearea if this seems dodge (that is how it is done in IHACRES)
        #TODO should these all just be tanks?
        self.subsurface_residence_time = 2
        self.subsurface_runoff = self.empty_vqip()
        
        self.percolation_residence_time = 10
        self.percolation = self.empty_vqip()
        
        self.surface_residence_time = 1
        self.surface_runoff = self.empty_vqip()
        
        
        self.surfaces = surfaces
        
        self.running_inflow_mb = self.empty_vqip()
        self.running_outflow_mb = self.empty_vqip()
        
        self.mass_balance_in.append(lambda : self.running_inflow_mb)
        self.mass_balance_out.append(lambda : self.running_outflow_mb)

    def run(self):
        for surface in self.surfaces:
            surface.run()
            
        #Apply residence time to percolation
        percolation = self.percolation
        percolation = self.v_change_vqip(percolation, percolation['volume'] / self.percolation_residence_time)
        
        #Distribute percolation
        reply = self.push_distributed(percolation, of_type = ['Groundwater'])
        
        #Update percolation 'tank'
        net_percolation = self.extract_vqip(percolation, reply)
        self.percolation = self.extract_vqip(self.percolation, net_percolation)
        
        #Apply residence time to subsurface/surface runoff
        surface_runoff = self.surface_runoff
        surface_runoff = self.v_change_vqip(surface_runoff, surface_runoff['volume'] / self.surface_runoff_residence_time)
        subsurface_runoff = self.subsurface_runoff
        subsurface_runoff = self.v_change_vqip(subsurface_runoff, subsurface_runoff['volume'] / self.subsurface_runoff_residence_time)
        
        #Distributed total runoff
        total_runoff = self.sum_vqip(surface_runoff, subsurface_runoff)
        reply = self.push_distributed(total_runoff, of_type = ['River','Node'])
        
        #Update surface/subsurface runoff 'tanks'
        net_surface_runoff = self.extract_vqip(surface_runoff, self.v_change_vqip(reply, reply['volume'] * surface_runoff['volume'] / total_runoff['volume']) )
        net_subsurface_runoff = self.extract_vqip(subsurface_runoff, self.v_change_vqip(reply, reply['volume'] * subsurface_runoff['volume'] / total_runoff['volume']) )
        self.surface_runoff = self.extract_vqip(self.surface_runoff, net_surface_runoff)
        self.subsurface_runoff = self.extract_vqip(self.subsurface_runoff, net_subsurface_runoff)        
        
    def get_data_input(self, var):
        return self.data_input_dict[(var, self.t)]
    
    def end_timestep(self):
        self.running_inflow_mb = self.empty_vqip()
        self.running_outflow_mb = self.empty_vqip()
        
class Surface(DecayTank):
    def __init__(self, **kwargs):
        #TODO EVERYONE INHERITS THIS DEPTH VALUE... FIX THAT
        self.depth = 0
        self.decays = {} #generic decay parameters
        
        #Parameters
        super().__init__(**kwargs)        
        
        self.capacity = self.depth * self.area   
        
        self.inflows = [self.atmospheric_deposition,
                        self.precipitation_deposition]
        self.processes = [lambda x: None]
        self.outflows = [lambda x: None]
        
    def run(self):
        
        for f in self.inflows:
            in_, out_ = f()
            self.running_inflow_mb = self.sum_vqip(self.running_inflow_mb, in_)
            self.running_outflow_mb = self.sum_vqip(self.running_outflow_mb, out_)
        
        for f in self.processes + self.outflows:
            f()
            
        
    def get_data_input(self, var):
        return self.parent.get_data_input(var)
    
    def dry_deposition_to_tank(self, vqip):
        _ = self.push_storage(vqip, force = True)
        
    def wet_deposition_to_tank(self, vqip):
        _ = self.push_storage(vqip, force = True)

    def atmospheric_deposition(self):
        #TODO double check units - is weight of N or weight of NHX/NOX?
        nhx = self.get_data_input('nhx-dry') * self.area
        nox = self.get_data_input('nox-dry') * self.area
        
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = nox
        
        self.dry_deposition_to_tank(vqip)
        return (vqip, self.empty_vqip())
        
    def precipitation_deposition(self):
        #TODO double check units - is weight of N or weight of NHX/NOX?
        nhx = self.get_data_input('nhx-wet') * self.area
        nox = self.get_data_input('nox-wet') * self.area
        
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = nox
        
        self.wet_deposition_to_tank(vqip)
        return (vqip, self.empty_vqip())
    
class ImperviousSurface(Surface):
    def __init__(self, **kwargs):
        self.pore_depth = 0 #Need a way to say 'depth means pore depth'
        kwargs['depth'] = kwargs['pore_depth'] # TODO Need better way to handle this
        
        #Default parameters 
        self.et0_to_e = 0.1 #Total evaporation (ignoring transpiration)
        self.deposition_dict = {x : 0.001 for x in constants.POLLUTANTS} #kg/m2/dt
        
        
        super().__init__(**kwargs)
        
        self.inflows.append(self.urban_deposition)
        self.inflows.append(self.precipitation_evaporation)
        
        self.outflows.append(self.push_to_sewers)
    
    def urban_deposition(self):
        pollution = self.copy_vqip(self.pollutant_dict)
        pollution['volume'] = 0
        _ = self.push_storage(pollution, force = True)
        
        return (pollution, self.empty_vqip())
    
    def precipitation_evaporation(self):
        precipitation_depth = self.get_data_input('precipitation')
        evaporation_depth = self.get_data_input('et0') * self.et0_to_e
        
        if precipitation_depth < evaporation_depth:
            net_precipitation = 0
            evaporation_from_pores = evaporation_depth - precipitation_depth
            evaporation_from_pores *= self.area
            evaporation_from_pores = self.evaporate(evaporation_from_pores)
            total_evaporation = evaporation_from_pores + precipitation_depth * self.area
        else:
            net_precipitation = precipitation_depth - evaporation_depth
            net_precipitation *= self.area
            net_precipitation = self.v_change_vqip(self.empty_vqip(), net_precipitation)
            _ = self.push_storage(net_precipitation)
            total_evaporation = evaporation_depth * self.area
        
        total_evaporation = self.v_change_vqip(self.empty_vqip(), total_evaporation)
        total_precipitation = self.v_change_vqip(self.empty_vqip(), precipitation_depth * self.area)
        
        return (total_precipitation, total_evaporation)
        
    
    def push_to_sewers(self):
        surface_runoff = self.pull_ponded()
        reply = self.parent.push_distributed(surface_runoff, of_type = ['Sewer'])
        _ = self.push_storage(reply, force = True)
        #TODO in cwsd_partition this is done with timearea
    
class PerviousSurface(Surface):
    def __init__(self, **kwargs):
        self.field_capacity = 0 #depth of water when water level is above this, recharge/percolation are generated
        self.wilting_point = 0 #Depth of tank when added to field capacity, water below this level is available for plants+evaporation but not drainage
        self.infiltration_capacity = 0 #depth of precipitation that can enter tank per timestep
        self.percolation_coefficient = 0 #proportion of water above field capacity that can goes to percolation
        self.et0_coefficient = 0.5 #proportion of et0 that goes to evapotranspiration
        self.ihacres_p = 0.5
        
        #TODO what should these params be?
        self.soil_temp_w_prev = 0.3 #previous timestep weighting
        self.soil_temp_w_air = 0.3 #air temperature weighting
        self.soil_temp_cons = 3 #deep soil temperature * weighting
        
        #IHACRES is a deficit not a tank, so doesn't really have a capacity in this way... and if it did.. it probably wouldn't be the sum of these
        kwargs['depth'] = kwargs['field_capacity'] + kwargs['wilting_point'] # TODO Need better way to handle this
        
        super().__init__(**kwargs)
        
        self.subsurface_coefficient = 1 - self.percolation_coefficient #proportion of water above field capacity that can goes to subsurface flow
        
        self.inflows.append(self.ihacres) #work out runoff
        
        self.processes.append(self.calculate_soil_temperature) # Calculate soil temp + dependence factor
        # self.processes.append(self.decay) #apply generic decay (currently handled by decaytank at end of timestep)
        
        # self.outflows.append(self.push_to_rivers)
    
    def ihacres(self):
        
        #Read data
        precipitation_depth = self.get_data_input('precipitation')
        evaporation_depth = self.get_data_input('et0') * self.et0_coefficient
        
        #Apply infiltration
        infiltrated_precipitation = min(precipitation_depth, self.infiltration_capacity)
        infiltration_excess = precipitation_depth - evaporation_depth - infiltrated_precipitation
        
        #Formulate in terms of (m) moisture deficit
        current_moisture_deficit_depth = self.get_excess() / self.area
        
        #IHACRES equations
        evaporation = evaporation_depth * min(1, exp(2 * (1 - current_moisture_deficit_depth / self.wilting_point)))
        outflow = infiltrated_precipitation  * (1 - min(1, (current_moisture_deficit_depth / self.field_capacity) ** self.ihacres_p))
        
        #Convert to volumes
        percolation = outflow * self.percolation_coefficient * self.area
        subsurface_flow = outflow * self.subsurface_coefficient * self.area
        tank_recharge = (infiltrated_precipitation - evaporation - outflow) * self.area
        infiltration_excess *= self.area
        evaporation *= self.area
        precipitation = precipitation_depth * self.area
        
        #Mix in tank to calculate pollutant concentrations
        total_water_passing_through_soil_tank = tank_recharge + subsurface_flow + percolation
        total_water_passing_through_soil_tank = self.v_change_vqip(self.empty_vqip(), total_water_passing_through_soil_tank)
        _ = self.push_storage(total_water_passing_through_soil_tank, force = True)
        subsurface_flow = self.pull_storage(subsurface_flow)
        percolation = self.pull_storage(percolation)
        
        #Convert to VQIPs
        infiltration_excess = self.v_change_vqip(self.empty_vqip(), infiltration_excess)
        precipitation = self.v_change_vqip(self.empty_vqip(), precipitation)
        evaporation = self.v_change_vqip(self.empty_vqip(), evaporation)
        
        #Send water 
        self.parent.surface_runoff = self.sum_vqip(self.parent.surface_runoff, infiltration_excess)
        self.parent.subsurface_runoff = self.sum_vqip(self.parent.subsurface_runoff, subsurface_flow)
        self.parent.percolation = self.sum_vqip(self.parent.surface_runoff, percolation)
        
        #Mass balance
        in_ = precipitation
        out_ = evaporation
        
        return (in_, out_)
        
    # def precipitation_infiltration_evaporation(self):
    #     precipitation_depth = self.get_data_input('precipitation')
    #     evaporation_depth = self.get_data_input('et0') * self.et0_coefficient
        
    #     if precipitation_depth < evaporation_depth:
    #         net_precipitation = 0
    #         evaporation_from_soil = evaporation_depth - precipitation_depth
    #         evaporation_from_soil *= self.area
    #         evaporation_from_soil = self.evaporate(evaporation_from_soil)
    #         total_evaporation = evaporation_from_soil + precipitation_depth * self.area
    #     else:
    #         net_precipitation = precipitation_depth - evaporation_depth
    #         infiltrated_precipitation = min(net_precipitation, self.infiltration_capacity)
    #         infiltration_excess = net_precipitation - infiltrated_precipitation
            
    #         infiltrated_precipitation *= self.area
    #         infiltrated_precipitation = self.v_change_vqip(self.empty_vqip(), infiltrated_precipitation)
    #         _ = self.push_storage(infiltrated_precipitation)
    #         total_evaporation = evaporation_depth * self.area
            
    #         #TODO - what to do with this
    #         infiltration_excess *= self.area
        
    #     total_evaporation = self.v_change_vqip(self.empty_vqip(), total_evaporation)
    #     total_precipitation = self.v_change_vqip(self.empty_vqip(), precipitation_depth * self.area)
        
    #     return (total_precipitation, total_evaporation)
        
    
    def calculate_soil_temperature(self):
        auto = self.storage['temperature'] * self.soil_temp_w_prev
        air = self.get_data_input('temperature') * self.soil_temp_w_air
        self.soil_storage['temperature'] = auto + air + self.soil_temp_cons
    
    # def decay(self):
    #     pass
    
    

    def push_to_rivers(self):
        pass

class CropSurface(PerviousSurface):
    def __init__(self, **kwargs):
        self.stage_dates = [] #dates when crops are planted/growing/harvested
        self.crop_factor = [] #coefficient to do with ET, associated with stages
        self.ET_depletion_factor = 0 #To do with water availability, p from FAOSTAT
        self.rooting_depth = 0 #To do with water availability, Zr from FAOSTAT
        
        self.fraction_dry_deposition_to_DIN = 0.9 #TODO may or may not be handled in preprocessing
        self.nutrient_parameters = {}
        
        super().__init__(**kwargs)
        
        self.nutrient_pool = NutrientPool(**self.nutrient_parameters)
        self.fraction_dry_deposition_to_fast = 1 - self.fraction_dry_deposition_to_DIN
        self.inflows.append(self.fertiliser)
        self.inflows.append(self.manure)
        
        self.processes.append(self.soil_moisture_dependence_factor)
        self.processes.append(self.nutrient_pool.soil_pool_transformation)
        
        #TODO possibly move these into nutrient pool
        self.processes.append(self.suspension)
        self.processes.append(self.erosion)
        self.processes.append(self.denitrification)
        self.processes.append(self.adsorption)
    
    def soil_moisture_dependence_factor(self):
        pass
    
    def fertiliser(self):
        pass
    
    def manure(self):
        pass
    
    def suspension(self):
        pass
    
    def erosion(self):
        pass
    
    def denitrification(self):
        pass
    
    def adsorption(self):
        pass
    
    def dry_deposition_to_tank(self, vqip):
        #Distribute between surfaces
        #TODO INCLUDE P AND AMMONIA
        
        nitrate_to_din = vqip['nitrate'] * self.fraction_dry_deposition_to_DIN
        nitrate_to_fast = vqip['nitrate'] * self.fraction_dry_deposition_to_fast
        pass
    
    def wet_deposition_to_tank(self, vqip):
        pass

    
class IrrigationSurface(CropSurface):
    def __init__(self, **kwargs):
        self.irrigation_cover = 0 #proportion area irrigated
        self.irrigation_efficiency = 0 #proportion of demand met
        
        super().__init__(**kwargs)
        
        self.inflows.append(self.calculate_irrigation)
        self.inflows.append(self.satisfy_irrigation)
        
        self.processes.append(self.crop_uptake)
        
    def calculate_irrigation(self):
        pass
    
    def crop_uptake(self):
        pass
    
    def satisfy_irrigation(self):
        pass

class GardenSurface(IrrigationSurface):
    #TODO - probably a simplier version of this is useful, building just on pervioussurface
    def __init__(self, **kwargs):
        self.satisfy_irrigation = self.pull_from_distribution
        
        super().__init__(**kwargs)
        
    def pull_from_distribution(self):
        pass