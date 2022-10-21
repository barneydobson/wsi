# -*- coding: utf-8 -*-
"""
Created on Fri May 20 08:58:58 2022

@author: Barney
"""
from wsimod.nodes.nodes import Node, Tank, DecayTank, QueueTank, ResidenceTank
from wsimod.nodes.nutrient_pool import NutrientPool
from wsimod.core import constants
from math import exp, log, log10, sin
from bisect import bisect_left
import sys

class Land(Node):
    def __init__(self,
                        name,
                        subsurface_residence_time = 5,
                        percolation_residence_time = 50,
                        surface_residence_time = 1,
                        surfaces = [],
                        data_input_dict = {}):
        """An extensive node class that represents land processes (agriculture, soil, 
        subsurface flow, rural runoff, urban drainage, pollution deposition). The 
        expected use is that each distinctive type of land cover (different crop 
        types, gardens, forests, impervious urban drainage, etc.) each have a Surface 
        object, which is a subclass of Tank. The land node will iterate over its 
        surfaces each timestep, which will generally (except in the case of an 
        impervious surface) send water to three common Tanks: surface flow, 
        subsurface flow and percolation. These tanks will then send flows to rivers 
        or groundwater.
        
        (See wsimod/nodes/land.py/Surface and subclasses for currently available 
        surfaces)
        
        Args:
            name (str): node name.
            subsurface_residence_time (float, optional): Residence time for     
                subsurface flow (see nodes.py/ResidenceTank). Defaults to 5.
            percolation_residence_time (int, optional): Residence time for 
                percolation flow (see nodes.py/ResidenceTank). Defaults to 50.
            surface_residence_time (int, optional): Residence time for surface flow
                (see nodes.py/ResidenceTank). Defaults to 1.
            surfaces (list, optional): list of dicts where each dict describes the 
                parameters of each surface in the Land node. Each dict also contains 
                an entry under 'type_' which describes which subclass of surface to 
                use. Defaults to [].
            data_input_dict (dict, optional): Dictionary of data inputs relevant for 
                the node (generally, et0, precipitation and temperature). Keys are 
                tuples where first value is the name of the variable to read from the 
                dict and the second value is the time. Defaults to {}.

        Functions intended to call in orchestration:
            run
            apply_irrigation (if used)
        """
        #Assign parameters
        self.subsurface_residence_time = subsurface_residence_time
        self.percolation_residence_time = percolation_residence_time
        self.surface_residence_time = surface_residence_time
        self.data_input_dict = data_input_dict

        super().__init__(name, data_input_dict = data_input_dict)
        
        #This could be a deny but then you would have to know in advance whether a demand node has any gardening or not
        self.push_check_handler[('Demand','Garden')] = lambda x : self.empty_vqip()
        self.push_set_handler[('Demand','Garden')] = lambda x : self.empty_vqip()
        
        #Create surfaces
        self.irrigation_functions = [lambda : None]
        
        surfaces_ = surfaces.copy()
        surfaces = []
        for surface in surfaces_:
            #Assign parent (for data reading and to determine where to send flows to)
            surface['parent'] = self

            #Get surface type
            type_ = surface['type_']
            del surface['type_']

            #Instantiate surface and store in list of surfaces
            surfaces.append(getattr(sys.modules[__name__], type_)(**surface))

            #Assign ds (mass balance checking)
            self.mass_balance_ds.append(surfaces[-1].ds)

            #Assign any irrigation functions 
            if isinstance(surfaces[-1], IrrigationSurface):
                self.irrigation_functions.append(surfaces[-1].irrigate)
            
            #Assign garden surface functions
            if isinstance(surfaces[-1], GardenSurface):
                self.push_check_handler[('Demand','Garden')] = surfaces[-1].calculate_irrigation_demand
                self.push_set_handler[('Demand','Garden')] = surfaces[-1].receive_irrigation_demand
        
        #Update handlers
        self.push_set_handler['default'] = self.push_set_deny
        self.push_check_handler['default'] = self.push_check_deny
        self.push_set_handler['Sewer'] = self.push_set_sewer
        
        #Create subsurface runoff, surface runoff and percolation tanks
        #Can also do as timearea if this seems dodge (that is how it is done in IHACRES)
        #TODO should these be decayresidencetanks?
        self.subsurface_runoff = ResidenceTank(residence_time = self.subsurface_residence_time, 
                                               capacity = constants.UNBOUNDED_CAPACITY)
        self.percolation = ResidenceTank(residence_time = self.percolation_residence_time,
                                         capacity = constants.UNBOUNDED_CAPACITY)
        self.surface_runoff = ResidenceTank(residence_time = self.surface_residence_time,
                                            capacity = constants.UNBOUNDED_CAPACITY)
        
        #Store surfaces
        self.surfaces = surfaces
        
        #Mass balance checkign vqips and functions
        self.running_inflow_mb = self.empty_vqip()
        self.running_outflow_mb = self.empty_vqip()
        
        self.mass_balance_in.append(lambda : self.running_inflow_mb)
        self.mass_balance_out.append(lambda : self.running_outflow_mb)
        self.mass_balance_ds.append(self.surface_runoff.ds)
        self.mass_balance_ds.append(self.subsurface_runoff.ds)
        self.mass_balance_ds.append(self.percolation.ds)
    
    def apply_irrigation(self):
        """Iterate over any irrigation functions (needs further testing.. maybe)
        """
        for f in self.irrigation_functions:
            f()
            
    def run(self):
        """Call the run function in all surfaces, update surface/subsurface/
        percolation tanks, discharge to rivers/groundwater
        """

        #Run all surfaces
        for surface in self.surfaces:
            surface.run()
            
        #Apply residence time to percolation
        percolation = self.percolation.pull_outflow()
        
        #Distribute percolation
        reply = self.push_distributed(percolation, of_type = ['Groundwater'])
        
        if reply['volume'] > constants.FLOAT_ACCURACY:
            print('Groundwater rejected push')
            #Update percolation 'tank'
            _ = self.percolation.push_storage(reply, force = True)
        
        #Apply residence time to subsurface/surface runoff
        surface_runoff = self.surface_runoff.pull_outflow()
        subsurface_runoff = self.subsurface_runoff.pull_outflow()
        
        #Total runoff
        total_runoff = self.sum_vqip(surface_runoff, subsurface_runoff)
        if total_runoff['volume'] > 0:
            #Send to rivers (or nodes, which are assumed to be junctions)
            reply = self.push_distributed(total_runoff, of_type = ['River','Node'])
            
            #Redistribute total_runoff not sent
            if reply['volume'] > 0:
                reply_surface = self.v_change_vqip(reply, reply['volume'] * surface_runoff['volume'] / total_runoff['volume'])
                reply_subsurface = self.v_change_vqip(reply, reply['volume'] * subsurface_runoff['volume'] / total_runoff['volume'])
                
                #Update surface/subsurface runoff 'tanks'
                if reply_surface['volume'] > 0:
                    self.surface_runoff.push_storage(reply_surface, force = True)
                if reply_subsurface['volume'] > 0:
                    self.subsurface_runoff.push_storage(reply_subsurface, force = True)
    
    def push_set_sewer(self, vqip):
        """Receive water from a sewer and send it to the first ImperviousSurface in 
        surfaces. 

        Args:
            vqip (dict): A VQIP amount to be sent to the impervious surface

        Returns:
            vqip (dict): A VQIP amount of water that was not received
        """
        #TODO currently just push to the first impervious surface... not sure if people will be having multiple impervious surfaces. If people would be only having one then it would make sense to store as a parameter... this is probably fine for now
        for surface in self.surfaces:
            if isinstance(surface, ImperviousSurface):
                vqip = self.surface.push_storage(vqip, force = True)
                break
        return vqip
    
    def end_timestep(self):
        """Update mass balance and end timestep of all tanks (and surfaces)
        """
        self.running_inflow_mb = self.empty_vqip()
        self.running_outflow_mb = self.empty_vqip()
        for tanks in self.surfaces + [self.surface_runoff, self.subsurface_runoff, self.percolation]:
            tanks.end_timestep()
    
    def get_surface(self, surface_):
        """Return a surface from the list of surfaces by the 'surface' entry
        in the surface. I.e., the name of the surface

        Args:
            surface_ (str): Name of the surface

        Returns:
            surface (Surface): The first surface that matches the name
        """
        for surface in self.surfaces:
            if surface.surface == surface_:
                return surface
        return None
        
        
class Surface(DecayTank):
    def __init__(self,
                        surface = '',
                        area = 1,
                        depth = 1,
                        data_input_dict = {}, 
                        **kwargs):
        """A subclass of DecayTank. Each Surface is anticipated to represent a 
        different land cover type of a Land node. Besides functioning as a Tank, 
        Surfaces have three lists of functions (inflows, processes and outflows) 
        where behaviour can be added by appending new functions. We anticipate that 
        customised surfaces should be a subclass of Surface or its subclasses and add 
        functions to these lists. These lists are executed (inflows first, then 
        processes, then outflows) in the run function, which is called by the run 
        function in Land. The lists must return any model inflows or outflows as a 
        VQIP for mass balance checking.
        
        If a user wishes the DecayTank portion to be active, then can provide 
        'decays', which are passed upwards (see wsimod/core/core.py/DecayObj for 
        documentation)

        Args:
            surface (str, optional): String description of the surface type. Doesn't 
                serve a modelling purpose, just used for user reference. Defaults to ''.
            area (float, optional): Area of surface. Defaults to 1.
            depth (float, optional): Depth of tank (this has different physical 
                implications for different subclasses). Defaults to 1.
            data_input_dict (dict, optional):  Dictionary of data inputs relevant for 
                the surface (generally, deposition). Keys are tuples where first 
                value is the name of the variable to read from the dict and the 
                second value is the time. Note that this input should be specific to 
                the surface, and is not intended to be the same data input as for the 
                land node. Also note that with each surface having its own timeseries 
                of data inputs, this can take up a lot of memory, thus the default 
                behavior is to have this as monthly data where the time variable is a 
                monthyear. Defaults to {}.
        """
        #Assign parameters
        self.depth = depth
        self.data_input_dict = data_input_dict
        self.surface = surface
        #TODO this is a decaytank but growing surfaces don't have decay parameters... is it a problem.. we don't even take decays as an explicit argument and insert them in kwargs..
        capacity = area * depth
        #Parameters
        super().__init__(capacity = capacity,
                                area = area,
                                **kwargs)
        
        #Populate function lists
        #TODO.. not sure why I have deposition but no precipitation here
        #TODO - weird to have these deposition function if the relevant pollutants aren't modelled
        self.inflows = [self.atmospheric_deposition,
                        self.precipitation_deposition]
        self.processes = [lambda : (self.empty_vqip(), self.empty_vqip())]
        self.outflows = [lambda : (self.empty_vqip(), self.empty_vqip())]
        
        
    def run(self):
        """Call run function (called from Land node)
        """
        if 'nitrite' in constants.POLLUTANTS:
            #Assume that if nitrite is modelled then nitrification is also modelled
            #You will need ammonia->nitrite->nitrate decay to accurate simulate ammonia
            #Thus, these decays are simulated here

            #NOTE decay in a decaytank happens at start of timestep (confusingly) in the end_timestep function
            self.storage['nitrate'] += self.total_decayed['nitrite']
            self.parent.running_inflow_mb['nitrate'] += self.total_decayed['nitrite']
            
            #Decayed ammonia becomes nitrite
            self.storage['nitrite'] += self.total_decayed['ammonia']
            self.parent.running_inflow_mb['nitrite'] += self.total_decayed['ammonia']
        
        for f in self.inflows + self.processes + self.outflows:
            #Iterate over function lists, updating mass balance
            in_, out_ = f()
            self.parent.running_inflow_mb = self.sum_vqip(self.parent.running_inflow_mb, in_)
            self.parent.running_outflow_mb = self.sum_vqip(self.parent.running_outflow_mb, out_)
        
    def get_data_input(self, var):
        """Read data input from parent Land node (i.e., for precipitation/et0/temp)

        Args:
            var (str): Name of variable

        Returns:
            Data read
        """
        return self.parent.get_data_input(var)
    
    def get_data_input_surface(self, var):
        """Read data input from this surface's data_input_dict

        Args:
            var (str): Name of variable

        Returns:
            Data read
        """
        return self.data_input_dict[(var, self.parent.monthyear)]
    
    def dry_deposition_to_tank(self, vqip):
        """Generic function for allocating dry pollution deposition to the surface.
        Simply sends the pollution into the tank (some subclasses overwrite this 
        behaviour)

        Args:
            vqip (dict): A VQIP amount of dry deposition to send to tank

        Returns:
            vqip (dict): A VQIP amount of dry deposition that entered the tank (used 
                for mass balance checking)
        """
        #Default behaviour is to just enter the tank
        _ = self.push_storage(vqip, force = True)
        return vqip
        
    def wet_deposition_to_tank(self, vqip):
        """Generic function for allocating wet pollution deposition to the surface.
        Simply sends the pollution into the tank (some subclasses overwrite this 
        behaviour)

        Args:
            vqip (dict): A VQIP amount of wet deposition to send to tank

        Returns:
            vqip (dict): A VQIP amount of wet deposition that entered the tank (used 
                for mass balance checking)
        """
        #Default behaviour is to just enter the tank
        _ = self.push_storage(vqip, force = True)
        return vqip

    def atmospheric_deposition(self):
        """Inflow function to cause dry atmospheric deposition to occur, updating the 
        surface tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #TODO double check units in preprocessing - is weight of N or weight of NHX/noy?

        #Read data and scale
        nhx = self.get_data_input_surface('nhx-dry') * self.area
        noy = self.get_data_input_surface('noy-dry') * self.area
        srp = self.get_data_input_surface('srp-dry') * self.area
        
        #Assign pollutants
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = noy
        vqip['phosphate'] = srp
        
        #Update tank
        in_ = self.dry_deposition_to_tank(vqip)
        
        #Return mass balance
        return (in_, self.empty_vqip())
        
    def precipitation_deposition(self):
        """Inflow function to cause wet precipitation deposition to occur, updating 
        the surface tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #TODO double check units - is weight of N or weight of NHX/noy?

        #Read data and scale
        nhx = self.get_data_input_surface('nhx-wet') * self.area
        noy = self.get_data_input_surface('noy-wet') * self.area
        srp = self.get_data_input_surface('srp-wet') * self.area
        
        #Assign pollutants
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = noy
        vqip['phosphate'] = srp
        
        #Update tank
        in_ = self.wet_deposition_to_tank(vqip)

        #Return mass balance
        return (in_, self.empty_vqip())
    
class ImperviousSurface(Surface):
    def __init__(self,
                        pore_depth = 0,
                        et0_to_e = 1,
                        pollutant_load = {},
                        **kwargs):
        """A surface to represent impervious surfaces that drain to storm sewers. 
        Runoff is generated by the surface tank overflowing, if a user wants all 
        precipitation to immediately go to runoff then they should reduce 'pore_depth', 
        however generally this is not what happens and a small (a few mm) depth should 
        be assigned to the tank. Also includes urban pollution deposition, though this 
        will only be mobilised if runoff occurs. 
        
        Note that the tank does not have a runoff coefficient because it doesn't make 
        sense from an integrated perspective. If a user wants to mimic runoff 
        coefficient-like behaviour, then they should reduce the ImperviousSurface tank 
        size, and increase other surfaces of the parent land node accordingly.

        Args:
            pore_depth (float, optional): The depth of the tank that must be exceeded 
                to generate runoff. Intended to represent the pores in ashpalt that 
                water accumulates in before flowing. Defaults to 0.
            et0_to_e (float, optional): Multiplier applied to the parent's data 
                timeseries of et0 to determine how much evaporation takes place on the 
                ImperviousSurface. Defaults to 1.
            pollutant_load (dict, optional): A dict of different pollutant amounts that 
                are deposited on the surface (units are mass per area per timestep). 
                Defaults to {}.
        """
                        
        #Assign parameters 
        self.et0_to_e = et0_to_e #Total evaporation

        if len(pollutant_load) > 0:
            self.pollutant_load = pollutant_load
        else:
            self.pollutant_load = {x : 0.001 for x in constants.POLLUTANTS} #kg/m2/dt
        
        
        super().__init__(depth = pore_depth,**kwargs)

        #Initialise state variables
        self.evaporation = self.empty_vqip()
        self.precipitation = self.empty_vqip()

        #Populate function lists
        self.inflows.append(self.urban_deposition)
        self.inflows.append(self.precipitation_evaporation)
        
        self.outflows.append(self.push_to_sewers)
    
    
    def urban_deposition(self):
        """Inflow function to cause urban pollution deposition to occur, updating the 
        surface tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Copy pollutant_load
        pollution = self.copy_vqip(self.pollutant_load)

        #Scale by area
        for pol in pollution.keys():
            pollution[pol] *= self.area
        pollution['volume'] = 0
        
        #Update tank
        _ = self.push_storage(pollution, force = True)
        
        return (pollution, self.empty_vqip())
    
    def precipitation_evaporation(self):
        """Inflow function that is a simple rainfall-evaporation model, updating the 
        surface tank. All precipitation that is not evaporated is forced into the tank 
        (even though some of that will later be pushed to sewers) - this enables runoff 
        to mix with the accumulated pollutants in the surface pores.

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Read data in length units
        precipitation_depth = self.get_data_input('precipitation')
        evaporation_depth = self.get_data_input('et0') * self.et0_to_e
        
        if precipitation_depth < evaporation_depth:
            #No effective precipitation
            net_precipitation = 0

            #Calculate how much should be evaporated from pores
            evaporation_from_pores = evaporation_depth - precipitation_depth

            #Scale
            evaporation_from_pores *= self.area

            #Pull from tank
            evaporation_from_pores = self.evaporate(evaporation_from_pores)

            #Scale to get actual evaporation
            total_evaporation = evaporation_from_pores + precipitation_depth * self.area
        else:
            #Effective precipitation
            net_precipitation = precipitation_depth - evaporation_depth

            #Scale
            net_precipitation *= self.area
            net_precipitation = self.v_change_vqip(self.empty_vqip(), net_precipitation)

            #Assign a temperature value
            #TODO how hot is rain? No idea... just going to use surface air temperature
            net_precipitation['temperature'] = self.get_data_input('temperature')
            
            #Update tank
            _ = self.push_storage(net_precipitation, force = True)
            total_evaporation = evaporation_depth * self.area
        
        #Converrt to VQIP
        self.evaporation = self.v_change_vqip(self.empty_vqip(), total_evaporation)
        self.precipitation = self.v_change_vqip(self.empty_vqip(), precipitation_depth * self.area)
        
        return (self.precipitation, self.evaporation)
        
    
    def push_to_sewers(self):
        """Outflow function that distributes ponded water (i.e., surface runoff) to the 
        parent node's attached sewers

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Get runoff
        surface_runoff = self.pull_ponded()

        #Distribute
        #TODO in cwsd_partition this is done with timearea
        reply = self.parent.push_distributed(surface_runoff, of_type = ['Sewer'])
        
        #Update tank (forcing, because if the water can't go to the sewer, where else can it go)
        _ = self.push_storage(reply, force = True)
        #TODO... possibly this could flow to attached river or land nodes.. or other surfaces? I expect this doesn't matter for large scale models.. but may need to be revisited for detailed sewer models
        
        #Return empty mass balance because outflows are handled by parent
        return (self.empty_vqip(), self.empty_vqip())
    
class PerviousSurface(Surface):
    def __init__(self,
                        depth = 0.75,
                        field_capacity = 0.3,
                        wilting_point = 0.12,
                        infiltration_capacity = 0.5,
                        surface_coefficient = 0.05,
                        percolation_coefficient = 0.75,
                        et0_coefficient = 0.5,
                        ihacres_p = 10,
                        **kwargs):
        """A generic pervious surface that represents hydrology with the IHACRES model.

        Args:
            depth (float, optional): Soil tank (i.e., root) depth. Defaults to 0.75.
            field_capacity (float, optional): The field capacity IHACRES parameter
                (i.e., when water content in the soil tank is above this value - flows 
                of any kind can be generated). Defaults to 0.3.
            wilting_point (float, optional): The wilting point IHACRES parameter (i.e., 
                when water content content in the soil tank is above this value - 
                plants can uptake water and evaporation from the soil tank can occur). 
                Defaults to 0.12.
            infiltration_capacity (float, optional): Depth of water per day that can 
                enter the soil tank. Non infiltrated water will pond and travel as 
                surface runoff from the parent Land node. Defaults to 0.5.
            surface_coefficient (float, optional): If flow is generated, the proportion 
                of flow that goes to surface runoff. Defaults to 0.05.
            percolation_coefficient (float, optional): If flow is generated, then the 
                proportion of water that does not go to surface runoff that goes to 
                percolation (i.e., groundwater) - the remainder goes to subsurface 
                runoff. Defaults to 0.75.
            et0_coefficient (float, optional): Convert between the parent nodes data 
                timeseries et0 - and potential evaptranspiration per unit area for this 
                surface. Defaults to=0.5,
            ihacres_p (float, optional): The IHACRES p parameter. Unless it is an 
                ephemeral stream this parameter probably can stay high. Defaults to 10.
        """
       #Assign parameters (converting field capacity and wilting point to depth)
        self.field_capacity = field_capacity * depth
        self.wilting_point = wilting_point * depth
        self.infiltration_capacity = infiltration_capacity
        self.surface_coefficient = surface_coefficient
        self.percolation_coefficient = percolation_coefficient
        self.et0_coefficient = et0_coefficient
        self.ihacres_p = ihacres_p       
        
        #Parameters to determine how to calculate the temperature of outflowing water
        #TODO what should these params be?
        self.soil_temp_w_prev = 0.1 #previous timestep weighting
        self.soil_temp_w_air = 0.6 #air temperature weighting
        self.soil_temp_w_deep = 0.1 #deep soil temperature weighting
        self.soil_temp_deep = 10 #deep soil temperature
        
        #IHACRES is a deficit not a tank, so doesn't really have a capacity in this way... and if it did.. I don't know if it would be the root depth
        super().__init__(depth=depth,**kwargs)
        
        #Calculate subsurface coefficient
        self.subsurface_coefficient = 1 - self.percolation_coefficient 


        #Initiliase state variables
        self.infiltration_excess = self.empty_vqip()
        self.subsurface_flow = self.empty_vqip()
        self.percolation = self.empty_vqip()
        self.tank_recharge = 0
        self.evaporation = self.empty_vqip()
        self.precipitation = self.empty_vqip()
        
        #Populate function lists
        self.inflows.append(self.ihacres) #work out runoff
                
        #TODO interception if I hate myself enough?
        self.processes.append(self.calculate_soil_temperature) # Calculate soil temp + dependence factor

        # self.processes.append(self.decay) #apply generic decay (currently handled by decaytank at end of timestep)
        #TODO decaytank uses air temperature not soil temperature... probably need to just give it the decay function
        
        self.outflows.append(self.route)
    
    def get_cmd(self):
        """Calculate moisture deficit (i.e., the tank excess converted to depth)

        Returns:
            (float): current moisture deficit
        """
        return self.get_excess()['volume'] / self.area
    
    def get_smc(self):
        """Calculate moisture content (i.e., the tank volume converted to depth)

        Returns:
            (float): soil moisture content
        """
        #Depth of soil moisture
        return self.storage['volume'] / self.area
    
    def ihacres(self):
        """Inflow function that runs the IHACRES model equations, updates tanks, and 
        store flows in state variables (which are later sent to the parent land node in 
        the route function)

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        
        #Read data (leave in depth units since that is what IHACRES equations are in)
        precipitation_depth = self.get_data_input('precipitation')
        evaporation_depth = self.get_data_input('et0') * self.et0_coefficient
        temperature = self.get_data_input('temperature')
        
        #Apply infiltration
        infiltrated_precipitation = min(precipitation_depth, self.infiltration_capacity)
        infiltration_excess = max(precipitation_depth - infiltrated_precipitation, 0) 
        
        #Get current moisture deficit
        current_moisture_deficit_depth = self.get_cmd()
        
        #IHACRES equations (we do (depth - wilting_point or field capacity) to convert from a deficit to storage tank)
        evaporation = evaporation_depth * min(1, exp(2 * (1 - current_moisture_deficit_depth / (self.depth - self.wilting_point))))
        outflow = infiltrated_precipitation * (1 - min(1, (current_moisture_deficit_depth / (self.depth - self.field_capacity)) ** self.ihacres_p))
        
        #Can't evaporate more than available moisture
        evaporation = min(evaporation, precipitation_depth + self.get_smc())
        
        #Scale to volumes and apply proportions to work out percolation/surface runoff/subsurface runoff
        surface = outflow * self.surface_coefficient * self.area
        percolation = outflow * (1-self.surface_coefficient) * self.percolation_coefficient * self.area
        subsurface_flow = outflow * (1-self.surface_coefficient) * self.subsurface_coefficient * self.area
        tank_recharge = (infiltrated_precipitation - evaporation - outflow) * self.area
        infiltration_excess *= self.area
        infiltration_excess += surface
        evaporation *= self.area
        precipitation = precipitation_depth * self.area
        
        #Mix in tank to calculate pollutant concentrations
        total_water_passing_through_soil_tank = tank_recharge + subsurface_flow + percolation
        
        if total_water_passing_through_soil_tank > 0:
            #Net effective preipitation
            total_water_passing_through_soil_tank = self.v_change_vqip(self.empty_vqip(), total_water_passing_through_soil_tank)
            #Assign a temperature before sending into tank
            total_water_passing_through_soil_tank['temperature'] = temperature
            #Assign to tank
            _ = self.push_storage(total_water_passing_through_soil_tank, force = True)
            #Pull flows (which now have nonzero pollutant concentrations)
            subsurface_flow = self.pull_storage({'volume': subsurface_flow})
            percolation = self.pull_storage({'volume':percolation})
        else:
            #No net effective precipitation (instead evaporation occurs)
            evap = self.evaporate(-total_water_passing_through_soil_tank)
            subsurface_flow = self.empty_vqip()
            percolation = self.empty_vqip()
            
            if abs(evap + infiltrated_precipitation * self.area - evaporation - infiltration_excess) > constants.FLOAT_ACCURACY:
                print('inaccurate evaporation calculation')
        
        #TODO saturation excess (think it should just be 'pull_ponded'  presumably in net effective precipitation? )
        
        #Convert to VQIPs
        infiltration_excess = self.v_change_vqip(self.empty_vqip(), infiltration_excess)
        infiltration_excess['temperature'] = temperature
        precipitation = self.v_change_vqip(self.empty_vqip(), precipitation)
        evaporation = self.v_change_vqip(self.empty_vqip(), evaporation)
        
        #Track flows (these are sent onwards in the route function)
        self.infiltration_excess = infiltration_excess
        self.subsurface_flow = subsurface_flow
        self.percolation = percolation
        self.tank_recharge = tank_recharge
        self.evaporation = evaporation
        self.precipitation = precipitation
        
        #Mass balance
        in_ = precipitation
        out_ = evaporation
        
        return (in_, out_)
    
    def route(self):
        """An outflow function that sends percolation, subsurface runoff and surface runoff to their respective tanks in the parent land node.

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        
        self.parent.surface_runoff.push_storage(self.infiltration_excess, force = True)
        self.parent.subsurface_runoff.push_storage(self.subsurface_flow, force = True)
        self.parent.percolation.push_storage(self.percolation, force = True)
        
        return (self.empty_vqip(), self.empty_vqip())
        
    
    def calculate_soil_temperature(self):
        """Process function that calculates soil temperature based on a weighted 
        average. This equation is from Lindstrom, Bishop & Lofvenius (2002), 
        hydrological processes - but it is not clear what the parameters should be.

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        auto = self.storage['temperature'] * self.soil_temp_w_prev
        air = self.get_data_input('temperature') * self.soil_temp_w_air
        total_weight = self.soil_temp_w_air + self.soil_temp_w_deep + self.soil_temp_w_prev
        self.storage['temperature'] = (auto + air + self.soil_temp_deep * self.soil_temp_w_deep)/total_weight
        
        return (self.empty_vqip(), self.empty_vqip())  

class GrowingSurface(PerviousSurface):
    def __init__(self,
                       rooting_depth = 0,
                        ET_depletion_factor = 0,
                        crop_factor_stages = [0,0,0,0,0,0], 
                        crop_factor_stage_dates = [0, 50, 200, 300, 301, 365], 
                        sowing_day = 1,
                        harvest_day = 365,
                        initial_soil_storage = None,
                       **kwargs
                        ):
        """Extensive surface subclass that implements the CatchWat equations (Liu, 
        Dobson & Mijic (2022) Science of the total environment), which in term are 
        primarily based on FAO document: 
        https://www.fao.org/3/x0490e/x0490e0ehtm#soil%20water%20availability. 
        This surface is a pervious surface that also has things that grow on it. This 
        behaviour includes soil nutrient pools, crop planting/harvest calendars, 
        erosion, crop behaviour.

        A key complexity of this surface is the nutrient pool (see wsimod/nodes/
        nutrient_pool.py), which is a class that tracks the amount of phosphorus and 
        nitrogen in different states and performs transformations that occur in the 
        phosphorus/nitrogen cycle. It is assumed that the phosphate/nitrate/nitrite/
        ammonia amounts in this Surface tank should track the dissolved inorganic pool 
        in the nutrient pool. Meanwhile, the org-phosphorus/org-nitrogen amounts in 
        this tank should track the dissolved organic pool in the nutrient pool. The 
        total amount of pollutants that enter this tank may not be the same as the 
        total amount that leave, because pollutants are transformed between inorganic/
        organic and between wet/dry states - these transformations are accounted for 
        in mass balance.

        For users to quickly enable/disable these nutrient processes, which are 
        computationally intensive (in current case studies they account for about half 
        of the total runtime), they are only active if 'nitrate' is one of the 
        modelled pollutants. Note that the code will not check if nitrite/phosphate/
        org-phosphorus/org-nitrogen/ammonia are also included, but they should be if nitrate is included and otherwise the code will crash with a key error.

        Args:
            rooting_depth (float, optional): Depth of the soil tank (i.e., how deep do 
                crop roots go). Defaults to 0.
            ET_depletion_factor (float, optional): Average fraction of soil that can be 
                depleted from the root zone before moisture stress (reduction in ET) 
                occurs. Defaults to 0.
            crop_factor_stages (list, optional): Crop factor is a multiplier on et0, 
                more grown plants have higher transpiration and higher crop factors.   
                This list shows changing crop factor at different times of year in 
                relation to crop_factor_stage_dates. See wsimod/preprocessing/
                england_data_formatting.py/format_surfaces for further details on 
                formulating these - since the interpolation used to find crop_factors 
                in between the given values in the list is a bit involved. Defaults to 
                [0,0,0,0,0,0].
            crop_factor_stage_dates (list, optional): Dates associated with 
                crop_factor_stages. Defaults to [0, 50, 200, 300, 301, 365].
            sowing_day (int, optional): day of year that crops are sown. Defaults to 1.
            harvest_day (int, optional): day of year that crops are harvest. Defaults 
                to 365.
            initial_soil_storage (dict or float, optional): See wsimod/nodes/nodes.py/
                Tank for documentation. Defaults to None.
        """
        
        #Crop factors (set when creating object)
        self.ET_depletion_factor = ET_depletion_factor #To do with water availability, p from FAOSTAT
        self.rooting_depth = rooting_depth #maximum depth that plants can absorb, Zr from FAOSTAT
        depth = rooting_depth

        #Crop parameters
        self.crop_cover_max = 0.9 # [-] 0~1
        self.ground_cover_max = 0.3 # [-]
        #TODO... really I should just have this as an annual profile parameter and do away with interpolation etc.
        self.crop_factor_stages = crop_factor_stages
        self.crop_factor_stage_dates = crop_factor_stage_dates
        self.sowing_day = sowing_day
        self.harvest_day = harvest_day
        
        #Soil moisture dependence parameters
        self.satact = 0.6 # [-] for calculating soil_moisture_dependence_factor
        self.thetaupp = 0.12 # [-] for calculating soil_moisture_dependence_factor
        self.thetalow = 0.08 # [-] for calculating soil_moisture_dependence_factor
        self.thetapow = 1 # [-] for calculating soil_moisture_dependence_factorself.satact = 0.6 # [-] for calculating soil_moisture_dependence_factor
    
        #Crop uptake parameters
        self.uptake1 = 15 # [g/m2/y] shape factor for crop (Dissolved) Inorganic nitrogen uptake
        self.uptake2 = 1 # [-] shape factor for crop (Dissolved) Inorganic nitrogen uptake
        self.uptake3 = 0.02 # [1/day] shape factor for crop (Dissolved) Inorganic nitrogen uptake
        self.uptake_PNratio = 1/7.2 # [-] P:N during crop uptake
        
        #Erosion parameters
        self.erodibility = 0.0025 # [g * d / (J * mm)]
        self.sreroexp = 1.2 # [-] surface runoff erosion exponent
        self.cohesion = 1 # [kPa]
        self.slope = 5 # [-] every 100
        self.srfilt = 0.95 # [-] ratio of eroded sediment left in surface runoff after filtration
        self.macrofilt = 0.1 # [-] ratio of eroded sediment left in subsurface flow after filtration
        
        #Denitrification parameters
        self.limpar = 0.7 # [-] above which denitrification begins
        self.exppar = 2.5 # [-] exponential parameter for soil_moisture_dependence_factor_exp calculation
        self.hsatINs = 1 # [mg/l] for calculation of half-saturation concentration dependence factor
        self.denpar = 0.015 # [-] denitrification rate coefficient
        
        #Adsorption parameters
        self.adosorption_nr_limit = 0.00001
        self.adsorption_nr_maxiter = 20
        self.kfr = 153.7 # [1/kg] freundlich adsorption isoterm
        self.nfr = 1/2.6 # [-] freundlich exponential coefficient
        self.kadsdes = 0.03 # [1/day] adsorption/desorption coefficient
        
        #Other soil parameters
        self.bulk_density = 1300 # [kg/m3]        
        super().__init__(depth = depth,**kwargs)
                
        #Infer basic sow/harvest calendar        
        self.harvest_sow_calendar = [0, self.sowing_day, self.harvest_day, self.harvest_day + 1, 365]
        self.ground_cover_stages = [0,0,self.ground_cover_max,0,0]
        self.crop_cover_stages = [0,0,self.crop_cover_max,0,0]
        
        #This is just based on googling when is autumn...
        if self.sowing_day > 265:
            self.autumn_sow = True
        else:
            self.autumn_sow = False
        
        #State variables
        self.days_after_sow = None
        self.crop_cover = 0
        self.ground_cover = 0
        self.crop_factor = 0
        self.et0_coefficient = 1

        #Calculate parameters based on capacity/wp
        self.total_available_water = (self.field_capacity - self.wilting_point) * self.depth
        if self.total_available_water < 0:
            print('warning: TAW < 0...')
        self.readily_available_water = self.total_available_water * self.ET_depletion_factor
        
        #Initiliase nutrient pools
        self.nutrient_pool = NutrientPool()
        #Reflect initial water concentration in dissolved nutrient pools
        self.nutrient_pool.dissolved_inorganic_pool.storage['P'] = self.initial_storage['phosphate']
        self.nutrient_pool.dissolved_inorganic_pool.storage['N'] = self.initial_storage['nitrate'] + self.initial_storage['ammonia'] + self.initial_storage['nitrite']
        self.nutrient_pool.dissolved_organic_pool.storage['P'] = self.initial_storage['org-phosphorus']
        self.nutrient_pool.dissolved_organic_pool.storage['N'] = self.initial_storage['org-nitrogen']
        if initial_soil_storage:
            #Reflect initial nutrient stores in solid nutrient pools
            self.nutrient_pool.adsorbed_inorganic_pool.storage['P'] = initial_soil_storage['phosphate']
            self.nutrient_pool.adsorbed_inorganic_pool.storage['N'] = initial_soil_storage['ammonia'] + initial_soil_storage['nitrate'] + initial_soil_storage['nitrite']
            self.nutrient_pool.fast_pool.storage['N'] = initial_soil_storage['org-nitrogen']
            self.nutrient_pool.fast_pool.storage['P'] = initial_soil_storage['org-phosphorus']
        
        self.inflows.insert(0, self.calc_crop_cover)
        if 'nitrate' in constants.POLLUTANTS:
            #Populate function lists 
            self.inflows.append(self.fertiliser)
            self.inflows.append(self.manure)
            # self.inflows.append(self.residue)
            
            self.processes.append(self.calc_temperature_dependence_factor)
            self.processes.append(self.calc_soil_moisture_dependence_factor)
            self.processes.append(self.soil_pool_transformation)
            self.processes.append(self.calc_crop_uptake)
            
            # TODO possibly move these into nutrient pool
            self.processes.append(self.erosion)
            self.processes.append(self.denitrification)
            self.processes.append(self.adsorption)
    
    def pull_storage(self, vqip):
        """Pull water from the surface, updating the surface storage VQIP. Nutrient 
        pool pollutants (nitrate/nitrite/ammonia/phosphate/org-phosphorus/
        org-nitrogen) are removed in proportion to their amounts in the dissolved 
        nutrient pools, if they are simulated. Other pollutants are removed in proportion to their amount in the surface tank.

        Args:
            vqip (dict): VQIP amount to be pulled, (only 'volume' key is needed)

        Returns:
            reply (dict): A VQIP amount successfully pulled from the tank
        """
        
        if self.storage['volume'] == 0:
            return self.empty_vqip()
        
        #Adjust based on available volume
        reply = min(vqip['volume'], self.storage['volume'])
        
        #Update reply to vqip (get concentration for non-nutrients)
        reply = self.v_change_vqip(self.storage, reply)
                
        if 'nitrate' in constants.POLLUTANTS:
            #Update nutrient pool and get concentration for nutrients
            prop = reply['volume'] / self.storage['volume']
            nutrients = self.nutrient_pool.extract_dissolved(prop)
            reply['nitrate'] = nutrients['inorganic']['N'] * self.storage['nitrate'] / (self.storage['nitrate'] + self.storage['ammonia'])
            reply['ammonia'] = nutrients['inorganic']['N'] * self.storage['ammonia'] / (self.storage['nitrate'] + self.storage['ammonia'])
            reply['phosphate'] = nutrients['inorganic']['P']
            reply['org-phosphorus'] = nutrients['organic']['P']
            reply['org-nitrogen'] = nutrients['organic']['N']
        
        #Extract from storage
        self.storage = self.extract_vqip(self.storage, reply)
        
        return reply
    
    def quick_interp(self, x, xp, yp):
        """A simple version of np.interp to intepolate crop information on the fly

        Args:
            x (int): Current time (i.e., day of year)
            xp (list): Predefined times (i.e., list of days of year)
            yp (list): Predefined values associated with xp

        Returns:
            y (float): Interpolated value for current time
        """
        x_ind = bisect_left(xp, x)
        x_left = xp[x_ind - 1]
        x_right = xp[x_ind]
        dif = x - x_left
        y_left = yp[x_ind - 1]
        y_right = yp[x_ind]
        y = y_left + (y_right - y_left) * dif / (x_right - x_left)
        return y
    
    def calc_crop_cover(self):
        """Process function that calculates how much crop cover there is, assigns 
        whether crops are sown/harvested, and calculates et0_coefficient based on 
        growth stage of crops.

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Get current day of year
        doy = self.parent.t.dayofyear
        
        if self.parent.t.is_leap_year:
            #Hacky way to handle leap years
            if doy > 59:
                doy -= 1
            
        if self.days_after_sow is None:
            if self.parent.t.dayofyear == self.sowing_day:
                #sow
                self.days_after_sow = 0
        else:
            if self.parent.t.dayofyear == self.harvest_day:
                #harvest
                self.days_after_sow = None
                self.crop_factor = self.crop_factor_stages[0]
                self.crop_cover = 0
                self.ground_cover = 0
            else:
                #increment days since sow
                self.days_after_sow += 1
        
        #Calculate relevant parameters
        self.crop_factor = self.quick_interp(doy, self.crop_factor_stage_dates, self.crop_factor_stages)
        if self.days_after_sow:
            #Move outside of this if, if you want nonzero crop/ground cover outside of season
            self.crop_cover = self.quick_interp(doy, self.harvest_sow_calendar, self.crop_cover_stages)
            self.ground_cover = self.quick_interp(doy, self.harvest_sow_calendar, self.ground_cover_stages)
        
        root_zone_depletion = max(self.field_capacity - self.get_smc(),0)
        if root_zone_depletion < self.readily_available_water :
            crop_water_stress_coefficient = 1
        else:
            crop_water_stress_coefficient = max(0, (self.total_available_water - root_zone_depletion) /\
                                                ((1 - self.ET_depletion_factor) * self.total_available_water))
        
        self.et0_coefficient = crop_water_stress_coefficient * self.crop_factor
        
        return (self.empty_vqip(), self.empty_vqip())
    
    
    def adjust_vqip_to_liquid(self, vqip, deposition, in_):
        """Function to interoperate between surface tank and nutrient pool. Most 
        depositions are given in terms of ammonia/nitrate/phosphate - they are then 
        aggregated to total N or P to enter the nutrient pools. Depending on the 
        source of deposition these may transform (e.g., some go to dissolved and some 
        to solids) upon entering the nutrient pool. To reflect these transformations 
        in the soil tank, the amounts entering the soil tank are adjusted 
        proportionately.

        Args:
            vqip (dict): A VQIP amount of pollutants originally intended to enter the 
                soil tank
            deposition (dict): A dict with nutrients (N and P) as keys, showing the 
                total amount of nutrients entering the nutrient pool
            in_ (dict): A dict with nutrients as keys, showing the updated amount of 
                nutrients that entered the nutrient pool as dissolved pollutants

        Returns:
            vqip (dict): A VQIP amount of pollutants that have been scaled to account 
                for nutrient pool transformations
        """
        if 'nitrate' in constants.POLLUTANTS:
            if deposition['N'] > 0:
                vqip['nitrate'] *= (in_['N'] / deposition['N'])
                vqip['ammonia'] *= (in_['N'] / deposition['N'])
                vqip['org-nitrogen'] *= (in_['N'] / deposition['N'])
            if deposition['P'] > 0:
                vqip['phosphate'] *= (in_['P'] / deposition['P'])
                vqip['org-phosphorus'] *= (in_['P'] / deposition['P'])
            
        return vqip
    
    def fertiliser(self):
        """Read, scale and allocate fertiliser, updating the tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        
        #TODO tidy up fertiliser/manure/residue/deposition once preprocessing is sorted
        
        #Scale for surface
        nhx = self.get_data_input_surface('nhx-fertiliser') * self.area
        noy = self.get_data_input_surface('noy-fertiliser') * self.area
        srp = self.get_data_input_surface('srp-fertiliser') * self.area
        
        #Update as VQIP
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = noy
        vqip['phosphate'] = srp
        
        #Enter nutrient pool
        deposition = self.nutrient_pool.get_empty_nutrient()
        deposition['N'] = vqip['nitrate'] + vqip['ammonia']
        deposition['P'] = vqip['phosphate']
        in_ = self.nutrient_pool.allocate_fertiliser(deposition)
        
        #Update tank
        vqip = self.adjust_vqip_to_liquid(vqip, deposition, in_)
        self.push_storage(vqip, force = True)
    
        return (vqip, self.empty_vqip())
    
    def manure(self):
        """Read, scale and allocate manure, updating the tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Scale for surface
        nhx = self.get_data_input_surface('nhx-manure') * self.area
        noy = self.get_data_input_surface('noy-manure') * self.area
        srp = self.get_data_input_surface('srp-manure') * self.area
        
        #Formulate as VQIP
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx
        vqip['nitrate'] = noy
        vqip['phosphate'] = srp
        
        #Enter nutrient pool
        deposition = self.nutrient_pool.get_empty_nutrient()
        deposition['N'] = vqip['nitrate'] + vqip['ammonia']
        deposition['P'] = vqip['phosphate']
        in_ = self.nutrient_pool.allocate_manure(deposition)

        #Update tank
        vqip = self.adjust_vqip_to_liquid(vqip, deposition, in_)
        
        self.push_storage(vqip, force = True)
    
        return (vqip, self.empty_vqip())
    
    def residue(self):
        """Read, scale and allocate residue, updating the tank (NOT CURRENTLY USED 
        BECAUSE NO DATA SOURCES FOR RESIDUE CAN BE IDENTIFIED)

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        
        nhx = self.get_data_input_surface('nhx-residue') * self.area
        noy = self.get_data_input_surface('noy-residue') * self.area
        srp = self.get_data_input_surface('srp-residue') * self.area
        
        vqip = self.empty_vqip()
        vqip['ammonia'] = nhx * self.nutrient_pool.fraction_residue_to_fast['N']
        vqip['nitrate'] = noy * self.nutrient_pool.fraction_residue_to_fast['N']
        vqip['org-nitrogen'] = (nhx + noy) * self.nutrient_pool.fraction_residue_to_humus['N']
        vqip['phosphate'] = srp * self.nutrient_pool.fraction_residue_to_fast['P']
        vqip['org-phosphorus'] = srp * self.nutrient_pool.fraction_residue_to_humus['P']
        
        deposition = self.nutrient_pool.get_empty_nutrient()
        deposition['N'] = vqip['nitrate'] + vqip['ammonia'] + vqip['org-nitrogen']
        deposition['P'] = vqip['phosphate'] + vqip['org-phosphorus']
        
        in_ = self.nutrient_pool.allocate_residue(deposition)
        vqip = self.adjust_vqip_to_liquid(vqip, deposition, in_)
        
        self.push_storage(vqip, force = True)
    
        return (vqip, self.empty_vqip())
    
    def soil_pool_transformation(self):
        """A process function that run transformation functions in the nutrient pool 
        and updates the pollutant concentrations in the surface tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Initialise mass balance tracking variables
        in_ = self.empty_vqip()
        out_ = self.empty_vqip()
        
        #Get proportion of nitrogen that is nitrate in the soil tank
        #NOTE ignores nitrite - couldn't find enough information on it
        nitrate_proportion = self.storage['nitrate'] / (self.storage['nitrate'] + self.storage['ammonia'])
        
        #Run soil pool functions
        increase_in_dissolved_inorganic, increase_in_dissolved_organic = self.nutrient_pool.soil_pool_transformation()

        #Update tank and mass balance
        #TODO .. there is definitely a neater way to write  this
        if increase_in_dissolved_inorganic['N'] > 0:
            #Increase in inorganic nitrogen, rescale back to nitrate and ammonia
            in_['nitrate'] = increase_in_dissolved_inorganic['N'] * nitrate_proportion
            in_['ammonia'] = increase_in_dissolved_inorganic['N'] * (1 - nitrate_proportion)
        else:
            #Decrease in inorganic nitrogen, rescale back to nitrate and ammonia
            out_['nitrate'] = -increase_in_dissolved_inorganic['N'] * nitrate_proportion
            out_['ammonia'] = -increase_in_dissolved_inorganic['N'] * (1 - nitrate_proportion)
            
        if increase_in_dissolved_organic['N'] > 0:
            #Increase in organic nitrogen
            in_['org-nitrogen'] = increase_in_dissolved_organic['N']
        else:
            #Decrease in organic nitrogen
            out_['org-nitrogen'] = -increase_in_dissolved_organic['N']
        
        if increase_in_dissolved_inorganic['P'] > 0:
            #Increase in inorganic phosphate
            in_['phosphate'] = increase_in_dissolved_inorganic['P']
        else:
            #Decrease in inorganic phosphate
            out_['phosphate'] = -increase_in_dissolved_inorganic['P']

        if increase_in_dissolved_organic['P'] > 0:
            #Increase in organic phosphorus
            in_['org-phosphorus'] = increase_in_dissolved_organic['P']
        else:
            #Decrease in organic phosphorus
            out_['org-phosphorus'] = -increase_in_dissolved_organic['P']
        
        #Update tank with inputs/outputs of pollutants
        _ = self.push_storage(in_, force = True)
        out2_ = self.pull_pollutants(out_)

        if not self.compare_vqip(out_, out2_):
            print('nutrient pool not tracking soil tank')
        
        return (in_, out_)
    
    def calc_temperature_dependence_factor(self):
        """Process function that calculates the temperature dependence factor for the 
        nutrient pool (which impacts soil pool transformations)

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation
        if self.storage['temperature'] > 5:
            temperature_dependence_factor = 2 ** ((self.storage['temperature'] - 20) / 10)
        elif self.storage['temperature'] > 0:
            temperature_dependence_factor = self.storage['temperature'] / 5
        else:
            temperature_dependence_factor = 0
        self.nutrient_pool.temperature_dependence_factor = temperature_dependence_factor
        return (self.empty_vqip(), self.empty_vqip())
        
    def calc_soil_moisture_dependence_factor(self):        
        """Process function that calculates the soil moisture dependence factor for 
        the nutrient pool (which impacts soil pool transformations)

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation
        current_soil_moisture = self.get_smc()
        if current_soil_moisture  >= self.field_capacity: 
            self.nutrient_pool.soil_moisture_dependence_factor = self.satact
        elif current_soil_moisture <= self.wilting_point: 
            self.nutrient_pool.soil_moisture_dependence_factor = 0
        else:
            fc_diff = self.field_capacity - current_soil_moisture
            fc_comp = (fc_diff / (self.thetaupp * self.rooting_depth)) ** self.thetapow
            fc_comp = (1 - self.satact) * fc_comp + self.satact
            wp_diff = current_soil_moisture - self.wilting_point
            wp_comp = (wp_diff / (self.thetalow * self.rooting_depth)) ** self.thetapow
            self.nutrient_pool.soil_moisture_dependence_factor = min(1, wp_comp, fc_comp)
        return (self.empty_vqip(), self.empty_vqip())
    
    def calc_crop_uptake(self):
        """Process function that calculates how much nutrient crops uptake and updates 
        nutrient pool and surface tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation

        #Initialise
        N_common_uptake = 0
        P_common_uptake = 0
        
        if self.days_after_sow:
            #If there are crops

            days_after_sow = self.days_after_sow
            
            if self.autumn_sow:
                temp_func = max(0, min(1, (self.storage['temperature'] - 5) / 20))
                days_after_sow -= 25 #Not sure why this is (but it's in HYPE)
            else:
                temp_func = 1
            
            #Calculate uptake
            uptake_par = (self.uptake1 - self.uptake2) * exp(-self.uptake3 * days_after_sow) * temp_func
            if (uptake_par + self.uptake2) > 0 :
                N_common_uptake = self.uptake1 * self.uptake2 * self.uptake3 * uptake_par / ((self.uptake2 + uptake_par) ** 2)
            N_common_uptake *= constants.G_M2_TO_KG_M2
            P_common_uptake = N_common_uptake * self.uptake_PNratio
            uptake = {'P' : P_common_uptake,
                      'N' : N_common_uptake}
            crop_uptake = self.nutrient_pool.dissolved_inorganic_pool.extract(uptake)
            out_ = self.empty_vqip()
            
            # Assuming plants eat N and P as nitrate and phosphate
            out_['nitrate'] = crop_uptake['N'] 
            out_['phosphate'] = crop_uptake['P'] 
            
            out2_ = self.pull_pollutants(out_)
            if not self.compare_vqip(out_, out2_):
                print('nutrient pool not tracking soil tank')
                    
            return (self.empty_vqip(), out_)
        else:
            return (self.empty_vqip(), self.empty_vqip())
        
    def erosion(self):
        """Outflow function that erodes adsorbed/humus phosphorus and sediment and sends onwards to percolation/surface runoff/subsurface runoff

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation (which explains why my documentation is a bit ambiguous - because theirs is too)

        #Convert precipitation to MM since all the equations assume that
        precipitation_depth = self.get_data_input('precipitation') * constants.M_TO_MM

        #Calculate how much rain is mobilising erosion
        if precipitation_depth > 5:
            rainfall_energy = 8.95 + 8.44 * log10(precipitation_depth * (0.257 + sin(2 * constants.PI * ((self.parent.t.dayofyear - 70) / 365)) * 0.09) * 2)
            rainfall_energy *= precipitation_depth
            mobilised_rain = rainfall_energy * (1 - self.crop_cover) * self.erodibility
        else:
            mobilised_rain = 0

        #Calculate if any infiltration is mobilising erosion
        if self.infiltration_excess['volume'] > 0:
            mobilised_flow = (self.infiltration_excess['volume'] / self.area * constants.M_TO_MM * 365) ** self.sreroexp
            mobilised_flow *= (1 - self.ground_cover) * (1/(0.5 * self.cohesion)) * sin(self.slope / 100) / 365
        else:
            mobilised_flow = 0
        
        #Sum flows (not sure why surface runoff isn't included)
        #TODO I'm pretty sure it should be included here
        total_flows = self.infiltration_excess['volume'] + self.subsurface_flow['volume'] + self.percolation['volume'] #m3/dt + self.tank_recharge['volume'] (guess not needed)
        
        #Convert to MM/M2
        erodingflow = total_flows / self.area * constants.M_TO_MM
        
        #Calculate eroded sediment
        transportfactor = min(1, (erodingflow / 4) ** 1.3)
        erodedsed = 1000 * (mobilised_flow +  mobilised_rain) * transportfactor # [kg/km2]
        #TODO not sure what conversion this HYPE 1000 is referring to
        
        # soil erosion with adsorbed inorganic phosphorus and humus phosphorus (erodedP as P in eroded sediments and effect of enrichment)
        if erodingflow > 4 :
            enrichment = 1.5
        elif erodingflow > 0:
            enrichment = 4 - (4 - 1.5) * erodingflow / 4
        else:
            return (self.empty_vqip(), self.empty_vqip())
        
        #Get erodable phosphorus
        erodableP = self.nutrient_pool.get_erodable_P() / self.area * constants.KG_M2_TO_KG_KM2
        erodedP = erodedsed * (erodableP / (self.rooting_depth * constants.M_TO_KM * self.bulk_density * constants.KG_M3_TO_KG_KM3)) * enrichment # [kg/km2]
        
        #Convert to kg
        erodedP *= (self.area * constants.M2_TO_KM2) # [kg]
        erodedsed *= (self.area * constants.M2_TO_KM2) # [kg]
        
        #Allocate to different flows
        surface_erodedP = self.srfilt * self.infiltration_excess['volume'] / total_flows * erodedP # [kg]
        surface_erodedsed = self.srfilt * self.infiltration_excess['volume'] / total_flows * erodedsed # [kg]
        
        subsurface_erodedP = self.macrofilt * self.subsurface_flow['volume'] / total_flows * erodedP # [kg]
        subsurface_erodedsed = self.macrofilt * self.subsurface_flow['volume'] / total_flows * erodedsed # [kg]
        
        percolation_erodedP = self.macrofilt * self.percolation['volume'] / total_flows * erodedP # [kg]
        percolation_erodedsed = self.macrofilt * self.percolation['volume'] / total_flows * erodedsed # [kg]
        
        #Track mass balance
        in_ = self.empty_vqip()
        
        #Total eroded phosphorus 
        eff_erodedP = percolation_erodedP + surface_erodedP + subsurface_erodedP # [kg]
        if eff_erodedP > 0:
            #Update nutrient pool
            org_removed, inorg_removed = self.nutrient_pool.erode_P(eff_erodedP)
            total_removed = inorg_removed + org_removed 
            
            if abs(total_removed - eff_erodedP) > constants.FLOAT_ACCURACY:
                print('weird nutrients')

            #scale flows to split between inorganic and organic eroded P  
            self.infiltration_excess['org-phosphorus'] += (surface_erodedP * org_removed / eff_erodedP)
            self.subsurface_flow['org-phosphorus'] += (subsurface_erodedP * org_removed / eff_erodedP)
            self.percolation['org-phosphorus'] += (percolation_erodedP * org_removed / eff_erodedP)
            
            #TODO Leon reckons this is conceptually dodgy.. but i'm not sure where else adsorbed inorganic phosphorus should go
            self.infiltration_excess['phosphate'] += (surface_erodedP * inorg_removed / eff_erodedP)
            self.subsurface_flow['phosphate'] += (subsurface_erodedP * inorg_removed / eff_erodedP)
            self.percolation['phosphate'] += (percolation_erodedP * inorg_removed / eff_erodedP)
            
            #Entering the model (no need to uptake surface tank because both adsorbed inorganic pool and humus pool are solids and so no tracked in the soil water tank)
            in_['phosphate'] = inorg_removed
            in_['org-phosphorus'] = org_removed
        else:
            inorg_to_org_P = 0
        
        #Track sediment as solids
        self.infiltration_excess['solids'] += surface_erodedsed
        self.subsurface_flow['solids'] += subsurface_erodedsed
        self.percolation['solids'] += percolation_erodedsed

        
        in_['solids'] = surface_erodedsed + subsurface_erodedsed + percolation_erodedsed
        
        return (in_, self.empty_vqip())
    
    def denitrification(self):
        """Outflow function that performs denitirication processes, updating nutrient pool and soil tank

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation 
        #TODO could more of this be moved to NutrientPool
        #Calculate soil moisture dependence of denitrification
        soil_moisture_content = self.get_smc()
        if soil_moisture_content > self.field_capacity:
            denitrifying_soil_moisture_dependence = 1
        elif soil_moisture_content / self.field_capacity > self.limpar:
            denitrifying_soil_moisture_dependence = (((soil_moisture_content / self.field_capacity) - self.limpar) / (1 - self.limpar)) ** self.exppar
        else:
            denitrifying_soil_moisture_dependence = 0
            return (self.empty_vqip(), self.empty_vqip())
        
        #Get dissolved inorg nitrogen as a concentration and calculate factor
        din_conc = self.nutrient_pool.dissolved_inorganic_pool.storage['N'] / self.storage['volume'] # [kg/m3]
        din_conc *= constants.KG_M3_TO_MG_L
        half_saturation_concentration_dependence_factor = din_conc / (din_conc + self.hsatINs)
        
        #Calculate and extract dentrified nitrogen
        denitrified_N = self.nutrient_pool.dissolved_inorganic_pool.storage['N'] *\
                            half_saturation_concentration_dependence_factor *\
                                denitrifying_soil_moisture_dependence *\
                                    self.nutrient_pool.temperature_dependence_factor *\
                                        self.denpar
        denitrified_request = self.nutrient_pool.get_empty_nutrient()
        denitrified_request['N'] = denitrified_N
        denitrified_N = self.nutrient_pool.dissolved_inorganic_pool.extract(denitrified_request)
        
        #Leon reckons this should leave the model (though I think technically some small amount goes to nitrite)
        out_ = self.empty_vqip()
        out_['nitrate'] = denitrified_N['N'] 
        
        #Update tank
        out2_ = self.pull_pollutants(out_)
        if not self.compare_vqip(out_, out2_):
            print('nutrient pool not tracking soil tank')

        return (self.empty_vqip(), out_)
    
    def adsorption(self):
        """Outflow function that calculates phosphorus adsorption/desorptions and 
        updates soil tank and nutrient pools

        Returns:
            (tuple): A tuple containing a VQIP amount for model inputs and outputs 
                for mass balance checking. 
        """
        #Parameters/equations from HYPE documentation 
        #TODO could this be moved to the nutrient pool?

        #Initialise mass balance checking
        in_ = self.empty_vqip()
        out_ = self.empty_vqip()
        
        #Get total phosphorus in pool available for adsorption/desorption
        limit = self.adosorption_nr_limit
        ad_de_P_pool = self.nutrient_pool.adsorbed_inorganic_pool.storage['P'] + self.nutrient_pool.dissolved_inorganic_pool.storage['P'] # [kg]
        ad_de_P_pool /= (self.area * constants.M2_TO_KM2) # [kg/km2]
        if ad_de_P_pool == 0:
            return (self.empty_vqip(), self.empty_vqip())
        
        #Calculate coefficient and concentration of adsorbed phosphorus
        soil_moisture_content = self.get_smc() * constants.M_TO_MM # [mm] (not sure why HYPE has this in mm but whatever)
        conc_sol = self.nutrient_pool.adsorbed_inorganic_pool.storage['P'] * constants.KG_TO_MG / (self.bulk_density * self.rooting_depth * self.area)# [mg P/kg soil]
        coeff = self.kfr * self.bulk_density * self.rooting_depth * constants.M_TO_MM # [mm]
        
        # calculate equilibrium concentration
        if conc_sol <= 0 :
            #Not sure how this would happen
            print('Warning: soil partP <=0. Freundlich will give error, take shortcut.')
            xn_1 = ad_de_P_pool / (soil_moisture_content + coeff) # [mg/l]
            ad_P_equi_conc = self.kfr * xn_1   # [mg/ kg]
        else:
            # Newton-Raphson method
            x0 = exp((log(conc_sol) - log(self.kfr)) / self.nfr) # initial guess of equilibrium liquid concentration
            fxn = x0 * soil_moisture_content + coeff * (x0 ** self.nfr) - ad_de_P_pool
            xn = x0
            xn_1 = xn
            j = 0
            while (abs(fxn) > limit and j < self.adsorption_nr_maxiter) : # iteration to calculate equilibrium concentations
                fxn = xn * soil_moisture_content + coeff * (xn ** self.nfr) - ad_de_P_pool
                fprimxn = soil_moisture_content + self.nfr * coeff * (xn ** (self.nfr - 1))
                dx = fxn / fprimxn
                if abs(dx) < (0.000001 * xn):
                    #From HYPE... not sure what it means
                    break
                xn_1 = xn - dx
                if xn_1 <= 0 :
                    xn_1 = 1e-10
                xn = xn_1
                j += 1
            ad_P_equi_conc = self.kfr * (xn_1 ** self.nfr)
            #print(ad_P_equi_conc, conc_sol)
        
        # Calculate new pool and concentration, depends on the equilibrium concentration
        if abs(ad_P_equi_conc - conc_sol) > 1e-6 :
            request = self.nutrient_pool.get_empty_nutrient()
            
            #TODO not sure about this if statement, surely it would be triggered every time
            adsdes = (ad_P_equi_conc - conc_sol) * (1 - exp(-self.kadsdes)) # kinetic adsorption/desorption
            request['P'] = adsdes * self.bulk_density * self.rooting_depth * (self.area * constants.M2_TO_KM2) # [kg]
            if request['P'] > 0:
                #Adsorption
                adsorbed = self.nutrient_pool.dissolved_inorganic_pool.extract(request)
                if (adsorbed['P'] - request['P']) > constants.FLOAT_ACCURACY:
                    print('Warning: freundlich flow adjusted, was larger than pool')
                self.nutrient_pool.adsorbed_inorganic_pool.receive(adsorbed)
                
                #Dissolved leaving the soil water tank and becoming solid
                out_['phosphate'] = adsorbed['P']
                
                #Update tank
                out2_ = self.pull_pollutants(out_)
                if not self.compare_vqip(out_, out2_):
                    print('nutrient pool not tracking soil tank')
            else:
                #Desorption
                request['P'] = -request['P']
                desorbed = self.nutrient_pool.adsorbed_inorganic_pool.extract(request)
                if (desorbed['P'] - request['P']) > constants.FLOAT_ACCURACY:
                    print('Warning: freundlich flow adjusted, was larger than pool')
                self.nutrient_pool.dissolved_inorganic_pool.receive(desorbed)
                
                #Solid phosphorus becoming inorganic P in the soil water tank
                in_['phosphate'] = desorbed['P']
                _ = self.push_storage(in_, force = True)
                
        return (in_, out_)
    
    def dry_deposition_to_tank(self, vqip):
        """Allocate dry deposition to surface tank, updating nutrient pool accordingly.

        Args:
            vqip (dict): A VQIP amount of dry deposition to send to tank

        Returns:
            vqip (dict): A VQIP amount of dry deposition that entered the tank (used 
                for mass balance checking)
        """

        #Convert to nutrients
        deposition = self.nutrient_pool.get_empty_nutrient()
        deposition['N'] = vqip['nitrate'] + vqip['ammonia']
        deposition['P'] = vqip['phosphate']

        #Update nutrient pool
        in_ = self.nutrient_pool.allocate_dry_deposition(deposition)
        vqip = self.adjust_vqip_to_liquid(vqip, deposition, in_)

        #Update tank
        self.push_storage(vqip, force = True)
        return vqip
        
    def wet_deposition_to_tank(self, vqip):
        """Allocate wet deposition to surface tank, updating nutrient pool accordingly.

        Args:
            vqip (dict): A VQIP amount of dry deposition to send to tank

        Returns:
            vqip (dict): A VQIP amount of dry deposition that entered the tank (used 
                for mass balance checking)
        """
        #Convert to nutrients
        deposition = self.nutrient_pool.get_empty_nutrient()
        deposition['N'] = vqip['nitrate'] + vqip['ammonia']
        deposition['P'] = vqip['phosphate']

        #Update nutrient pool
        in_ = self.nutrient_pool.allocate_wet_deposition(deposition)
        vqip = self.adjust_vqip_to_liquid(vqip, deposition, in_)

        #Update tank
        self.push_storage(vqip, force = True)
        return vqip
        
class IrrigationSurface(GrowingSurface):
    def __init__(self,irrigation_coefficient = 0.1,**kwargs):
        """A subclass of GrowingSurface that can calculate water demand for the crops 
        that is not met by precipitation and use the parent node to acquire water. 
        When the surface is created by the parent node, the irrigate function below is 
        assigned. 

        Args:
            irrigation_coefficient (float, optional): proportion area irrigated * 
                proportion of demand met. Defaults to 0.1.
        """
        #Assign param
        self.irrigation_coefficient = irrigation_coefficient #proportion area irrigated * proportion of demand met
        
        super().__init__(**kwargs)
               
    def irrigate(self):
        """Calculate water demand for crops and call parent node to acquire water, 
        updating surface tank and nutrient pools
        """
        if self.days_after_sow:
            #Irrigation is just difference between evaporation and precipitation amount
            irrigation_demand = max(self.evaporation['volume'] - self.precipitation['volume'], 0) * self.irrigation_coefficient
            if irrigation_demand > constants.FLOAT_ACCURACY:
                root_zone_depletion = self.get_cmd()
                if root_zone_depletion <= constants.FLOAT_ACCURACY:
                    #TODO this isn't in FAO... but seems sensible
                    irrigation_demand = 0
                
                #Pull water using parent node
                supplied = self.parent.pull_distributed({'volume' : irrigation_demand}, 
                                                         of_type = ['River',
                                                                    'Node',
                                                                    'Groundwater',
                                                                    'Reservoir'
                                                                    ])
                
                #update tank
                _ = self.push_storage(supplied, force = True)
                
                #update nutrient pools
                organic = {'N' : supplied['org-nitrogen'], 
                           'P' : supplied['org-phosphorus']}
                inorganic = {'N' : supplied['ammonia'] + supplied['nitrate'], 
                             'P' : supplied['phosphate']}
                self.nutrient_pool.allocate_organic_irrigation(organic)
                self.nutrient_pool.allocate_inorganic_irrigation(inorganic)


class GardenSurface(GrowingSurface):
    #TODO - probably a simplier version of this is useful, building just on pervioussurface
    def __init__(self, **kwargs):
        """A specific surface for gardens that treats the garden as a grass crop, but 
        that can calculate/receive irrigation through functions that are assigned by 
        the parent land node's handlers, which in turn are expected to be triggered by 
        a query from an attached Demand node.
        """
        super().__init__(**kwargs)
        
    def calculate_irrigation_demand(self,ignore_vqip = None):
        """A check function (assigned by parent to push check from demand nodes) that 
        calculations irrigation demand (i.e., difference between evaporation and 
        preciptiation)

        Args:
            ignore_vqip (any, optional): Conventional push checks send an optional 
                VQIP amount, however the intention with this check is to get the 
                irrigation demand

        Returns:
            reply (dict): A VQIP amount of irrigation demand (note only 'volume' key 
                is used)
        """
        #Calculate irrigation demand
        irrigation_demand = max(self.evaporation['volume'] - self.precipitation['volume'], 0)
        
        root_zone_depletion = self.get_cmd()
        if root_zone_depletion <= constants.FLOAT_ACCURACY:
            #TODO this isn't in FAO... but seems sensible
            irrigation_demand = 0
        
        #Reply as VQIP
        reply = self.empty_vqip()
        reply['volume'] = irrigation_demand
        return reply
        
    def receive_irrigation_demand(self, vqip):
        """A set function (assigned by parent to push set from demand nodes) that assigns irrigation water supply to the surface tank

        Args:
            vqip (dict): A VQIP amount of irrigation to receive

        Returns:
            (dict): A VQIP amount of irrigation that was not received (should always 
                be empty)
        """
        #update tank
        return self.push_storage(vqip, force = True)
            