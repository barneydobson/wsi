# -*- coding: utf-8 -*-
"""
Created on Mon Nov 15 14:20:36 2021

@author: bdobson

Converted to totals BD 2022-05-03

"""
from wsimod.nodes.nodes import Node
from wsimod.core import constants

class Demand(Node):
    def __init__(self,
                        name,
                        population = 1,
                        pollutant_load = {},
                        per_capita = 0.12,
                        gardening_efficiency = 0.6 * 0.7, #Watering efficiency by irrigated area
                        data_input_dict = {}, #For temperature
                        constant_temp = 30,
                        constant_weighting = 0.2,
                        ):
        """Node that generates and moves water associated with population. 

        Args:
            name (str): node name
            population (float, optional): population of node. Defaults to 1.
            pollutant_load (dict, optional): Mass per person per timestep of 
                different pollutants generated. Defaults to {}.
            per_capita (float, optional): Volume per person per timestep of water 
                used. Defaults to 0.12.
            gardening_efficiency (float, optional): Value between 0 and 1 that 
                translates irrigation demand from GardenSurface into water requested 
                from the distribution network. Should account for percent of garden 
                that is irrigated and the efficacy of people in meeting their garden 
                water demand. Defaults to 0.6*0.7.
            data_input_dict (dict, optional):  Dictionary of data inputs relevant for 
                the node (temperature). Keys are tuples where first value is the name of the variable to read from the dict and the second value is the 
                time. Defaults to {}
            constant_temp (float, optional): A constant temperature associated with 
                generated water. Defaults to 30
            constant_weighting (float, optional): Proportion of temperature that is 
                made up from by cconstant_temp. Defaults to 0.2.
        """
        #TODO should temperature be defined in pollutant dict?
        #TODO a lot of this should probably be moved to ResidentialDemand
        #Assign parameters
        self.gardening_efficiency = gardening_efficiency
        self.population = population
        self.per_capita = per_capita
        self.pollutant_load = pollutant_load
        self.constant_weighting = constant_weighting
        self.constant_temp = constant_temp
        
        #Update args
        super().__init__(name, data_input_dict = data_input_dict)
        #Update handlers
        self.push_set_handler['default'] = self.push_set_deny
        self.push_check_handler['default'] = self.push_check_deny
        self.pull_set_handler['default'] = self.pull_set_deny
        self.pull_check_handler['default'] = self.pull_check_deny
        
        #Initialise states
        self.total_demand = self.empty_vqip()
        self.total_backup = self.empty_vqip() #ew
        self.total_received = self.empty_vqip()
        
        #Mass balance
        # Because we assume demand is always satisfied
        # received water 'disappears' for mass balance
        # and consumed water 'appears' (this makes)
        # introduction of pollutants easy
        self.mass_balance_in.append(lambda : self.total_demand)
        self.mass_balance_out.append(lambda : self.total_backup)
        self.mass_balance_out.append(lambda : self.total_received) 
        
    def create_demand(self):
        """Function to call get_demand, which should return a dict with keys that 
        match the keys in directions. A dict that determines how to push_distributed 
        the generated wastewater/garden irrigation. Water is drawn from attached 
        nodes.
        """
        demand = self.get_demand()
        total_requested = 0
        for dem in demand.values():
            total_requested += dem['volume']
            
        self.total_received = self.pull_distributed({'volume' : total_requested})
        #TODO Currently just assume all water is received and then pushed onwards
        
        directions = {'garden' : {'tag' : ('Demand',
                                           'Garden'),
                                  'of_type' : 'Land'},
                      'house' : {'tag' : 'Demand',
                                 'of_type' : 'Sewer'}}
        
        
        #Send water where it needs to go
        for key, item in demand.items():
            
            #Distribute
            remaining = self.push_distributed(item,
                                              of_type = directions[key]['of_type'],
                                              tag = directions[key]['tag']
                                              )
            
            if remaining['volume'] > constants.FLOAT_ACCURACY:
                print('Demand not able to push')
                self.total_backup = self.sum_vqip(self.total_backup, remaining)
                
        #Update for mass balance
        for dem in demand.values():
            self.total_demand = self.sum_vqip(self.total_demand, 
                                              dem)
                
    def get_constant_demand(self):
        """Holder function to enable constant demand generation

        Returns:
            (dict): A VQIP that will contain constant demand
        """
        #TODO read/gen demand
        return self.empty_vqip()
    
    def end_timestep(self):
        """Reset state variable trackers
        """
        self.total_demand = self.empty_vqip()
        self.total_backup = self.empty_vqip()
        self.total_received = self.empty_vqip()
        
class NonResidentialDemand(Demand):
    """Holder class to enable non-residential demand generation
    """
        
    def get_demand(self):
        """Holder function to call get_constant_demand

        Returns:
            (dict): A dict of VQIPs, where the keys match with directions 
                in Demand/create_demand
        """
        return {'house' : self.get_constant_demand()}

class ResidentialDemand(Demand):
    """Subclass of demand with functions to handle internal and external water use
    """
    
    def get_demand(self):
        """Overwrite get_demand and replace with custom functions

        Returns:
            (dict): A dict of VQIPs, where the keys match with directions 
                in Demand/create_demand
        """
        water_output = {}        
                
        water_output['garden'] = self.get_garden_demand()        
        water_output['house'] = self.get_house_demand()      
        
        return water_output

    
    def get_garden_demand(self):
        """Calculate garden water demand in the current timestep by get_connected
        to all attached land nodes. This check should return garden water demand.
        Applies irrigation coefficient. Can function when a single population node is 
        connected to multiple land nodes, however, the capacity and preferences of 
        arcs should be updated to reflect what is possible based on area.

        Returns:
            vqip (dict): A VQIP of garden water use (including pollutants) to be 
                pushed to land
        """
        #Get garden water demand
        excess = self.get_connected(direction = 'push',
                                    of_type = 'Land', 
                                    tag = ('Demand', 
                                           'Garden')
                                    )['avail']
        
        #Apply garden_efficiency
        excess = self.excess_to_garden_demand(excess)

        #Apply any pollutants
        vqip = self.apply_gardening_pollutants(excess)
        return vqip
    
    def apply_gardening_pollutants(self, excess):
        """Holder function to apply pollutants (i.e., presumably fertiliser) to the garden.

        Args:
            excess (float): A volume of water applied to a garden

        Returns:
            (dict): A VQIP of water that includes pollutants to be sent to land
        """
        #TODO Fertilisers are currently applied in the land node... which is preferable?
        vqip = self.empty_vqip()
        vqip['volume'] = excess
        return vqip
        
        
    def excess_to_garden_demand(self, excess):
        """Apply garden_efficiency

        Args:
            excess (float): Volume of water required to satisfy garden irrigation

        Returns:
            (float): Amount of water actually applied to garden
        """
        #TODO Anything more than this needed?
        # (yes - population presence if eventually included!)
        
        return excess * self.gardening_efficiency
    
    def get_house_demand(self):
        """Per capita calculations for household wastewater generation. Applies 
        weighted temperature calculation

        Returns:
            (dict): A VQIP containg foul water
        """
        #TODO water that is consumed but not sent onwards as foul
        #Total water required
        consumption = self.population * self.per_capita
        #Apply pollutants
        foul = self.copy_vqip(self.pollutant_load)
        #Scale to population
        for pol in constants.ADDITIVE_POLLUTANTS:
            foul[pol] *= self.population
        #Update volume and temperature (which is weighted based on air temperature and constant_temp)
        foul['volume'] = consumption
        foul['temperature'] = self.get_data_input('temperature') * (1 - self.constant_weighting) + self.constant_temp * self.constant_weighting
        return foul