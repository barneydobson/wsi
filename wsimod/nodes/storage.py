# -*- coding: utf-8 -*-
"""
Created on Mon Nov 15 14:20:36 2021

@author: bdobson
"""
from wsimod.nodes.nodes import Node, Tank, QueueTank
from wsimod.core import constants

class Storage(Node):
    #Basically a wrapper for a tank

    def __init__(self, **kwargs):
        self.initial_storage = 0
        self.storage = 0
        self.area = 0
        self.datum = 10
        
        self.travel_time = 0
        
        super().__init__(**kwargs)
        
        #Create tank
        self.tank = Tank(capacity = self.storage,
                            area = self.area,
                            datum = self.datum,
                            )
        
        self.tank.storage['volume'] = self.initial_storage # TODO Automate this better
        self.tank.storage_['volume'] = self.initial_storage # TODO Automate this better
        
        #Update handlers
        self.push_set_handler['default'] = self.push_set_storage
        self.pull_set_handler['default'] = lambda vol : self.tank.pull_storage(vol)
        self.push_check_handler['default'] = self.tank.get_excess
        self.pull_check_handler['default'] = self.tank.get_avail
        
        #Mass balance
        self.mass_balance_ds.append(lambda : self.tank.ds())
    
    def push_set_storage(self, vqip):

        #Update tank
        reply = self.tank.push_storage(vqip)
            
        return reply
    
    def distribute(self):
        #Distribute any active storage
        storage = self.tank.pull_storage(self.tank.get_avail())
        retained = self.push_distributed(storage)
        if retained['volume'] > constants.FLOAT_ACCURACY:
            print('Storage unable to push')
            
    
    def end_timestep(self):
        self.tank.end_timestep()
    
    def reinit(self):
        # TODO Automate this better
        self.tank.reinit()
        self.tank.storage['volume'] = self.initial_storage
        self.tank.storage_['volume'] = self.initial_storage 

class Groundwater(Storage):
    def __init__(self, **kwargs):
        self.timearea = {0 : 1}
        
        super().__init__(**kwargs)
        self.push_set_handler['default'] = self.push_set_timearea
        self.tank = QueueTank(capacity = self.storage,
                                             area = self.area,
                                             datum = self.datum,
                                             )
    def push_set_timearea(self, vqip):
        reply = self.empty_vqip()
        #Iterate over timearea diagram
        for time, normalised in self.timearea.items():
            vqip_ = self.v_change_vqip(vqip, 
                                       vqip['volume'] * normalised)
            reply_ = self.tank.push_storage(vqip_,
                                            time = time) # TODO Should this be forced?
            reply = self.blend_vqip(reply, reply_)
        return reply
        
    def distribute(self):
        _ = self.tank.internal_arc.update_queue(direction = 'push')
        
        remaining = self.push_distributed(self.tank.active_storage)
        
        if remaining['volume'] > constants.FLOAT_ACCURACY:
            print('Groundwater couldnt push all')
        
        #Update tank
        sent = self.tank.active_storage['volume'] - remaining['volume']
        sent = self.v_change_vqip(self.tank.active_storage,
                                  sent)
        reply = self.tank.pull_storage(sent)
        if (reply['volume'] - sent['volume']) > constants.FLOAT_ACCURACY:
            print('Miscalculated tank storage in discharge')
        
        
    
class Abstraction(Storage):
    """
    With a river - abstractions must be placed at abstraction points
    They must have accumulated all upstream flows that are available for abstraction before being abstracted from
    Once abstracted from, they can then distribute
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.mrf = 0
        self.pull_set_handler['default'] = self.pull_set_abstraction
    
    def pull_set_abstraction(self, vqip):
        #Calculate MRF before reply TODO
        reply = self.tank.pull_storage(vqip['volume'])
        return reply

class Reservoir(Storage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
    
    def make_abstractions(self):
        reply = self.pull_distributed(self.tank.get_excess())
        spill = self.tank.push_storage(reply)
        if spill['volume'] > constants.FLOAT_ACCURACY:
            print('Spill at reservoir')