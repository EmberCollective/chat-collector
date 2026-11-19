"""Functions for an authenticated upstream console; this module starts no server."""
from .reception import SchoolReception
from .__main__ import collect_once

def status(state_dir):
 service=SchoolReception(state_dir)
 try:return service.status()
 finally:service.close()

def catalog(state_dir):
 service=SchoolReception(state_dir)
 try:return service.catalog()
 finally:service.close()

def permission(state_dir,value):
 service=SchoolReception(state_dir)
 try:return service.configure(value)
 finally:service.close()
