# Introduction

This project is to act as a proxy between 1 or more Zendure home battery devices. It should use [zenSDK](https://github.com/Zendure/zenSDK/tree/main) for communicating with the devices. The point is to let >1 Zendure home battery devices act as one big one and divide requested power in a smart way over available devices.

## Implementation
Iterative, no big bang.

### Step 0
Basic project scaffolding.

### Step 1
Basic proxy, 1-1 passthrough with simple power division.

### Step 2
Make it smarter, depending on battery charge and available capacity, distribute power.

### Step 3
To be defined.

## Dev tools
* python 3.14
* uv
* mise for local tool installation
* pytest for unit tests

## Output
* home assistant addon
