# Uniform DGX Spark platform

Connects DeepSeek Harness to a Uniform model platform running on DGX Spark. The tools can inspect models and deployments, start or stop deployments, publish services to the LAN, and manage external model endpoints.

The plugin talks only to the platform HTTP API. It does not execute Docker commands and never writes the platform API key into this package.
