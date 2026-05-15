Your disk is 76% full on the root partition (34G of 48G used).

Main Space Consumers:

Docker Image: speech-recognizer:v1 — 6.65GB (the NeMo ASR container you rebuilt)
K3s/Rancher Agent — 11GB (Kubernetes runtime data)
ContainerD — 1.5GB (container runtime)
System packages — 4GB+ (/usr, system files)
Why it filled up:

The speech-recognizer image is very large because it includes PyTorch, NeMo toolkit, and all CUDA libraries
K3s stores container layers and runtime data in agent
Multiple image rebuilds can accumulate disk usage
Solutions: