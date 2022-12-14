# -*- coding: utf-8 -*-
"""
Created on Mon Nov 15 14:20:36 2021

@author: bdobson
"""
from wsimod.nodes.nodes import Node
from wsimod.core import constants
#TODO call this outlet not waste
class Waste(Node):
    def __init__(self, name):
        """Outlet node that can receive any amount of water by pushes

        Args:
            name (str): Node name
        
        Functions intended to call in orchestration:
            None
        """
        #Update args
        super().__init__(name)
        
        #Update handlers
        self.pull_set_handler['default'] = self.pull_set_deny
        self.pull_check_handler['default'] = self.pull_check_deny
        self.push_set_handler['default'] = self.push_set_accept
        self.push_check_handler['default'] = self.push_check_accept
        
        #Mass balance
        self.mass_balance_out.append(self.total_in)
        
    def push_set_accept(self, vqip):
        """Push set function that accepts all water

        Args:
            vqip (dict): A VQIP that has been pushed (ignored)

        Returns:
            (dict): An empty VQIP, indicating all water was received
        """

        return self.empty_vqip()
    
    def push_check_accept(self, vqip = None):
        """Push check function that accepts all water

        Args:
            vqip (dict, optional): A VQIP that has been pushed (ignored)

        Returns:
            (dict): VQIP or an unbounded capacity, indicating all water can be received
        """
        if not vqip:
            vqip = self.empty_vqip()
            vqip['volume'] = constants.UNBOUNDED_CAPACITY
        return vqip