"""Core package; import configuration and runtime entry points explicitly.

Keeping package initialization free of runtime imports lets deterministic rules
be used without loading configuration, model clients, processing or notification
code. Callers import their dependencies from the corresponding submodules.
"""
