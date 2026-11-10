# Running FLUX on Low/Mid RAM Systems (≤32GB)

The raw `FLUX.1-schnell` and `FLUX.1-dev` models are massive.
- **UNET:** ~17 GB (`fp8` version) to ~24 GB (`fp16`)
- **Text Encoders:** ~5 GB (`t5xxl_fp8_e4m3fn` + `clip_l`) to ~10 GB (`t5xxl_fp16` + `clip_l`)
- **VAE:** ~335 MB

When ComfyUI loads these raw `.safetensors` files, it maps them directly into system RAM before moving them to VRAM. On a machine with **32GB of RAM or less**, loading a 24GB model alongside the OS overhead (Chrome, Discord, Obsidian, OS services) will exhaust the memory pool.

This causes:
1. **100% CPU Usage** (The Linux kernel thrashes as it desperately pages RAM to the SSD swap file).
2. **System Freezes** or severe unresponsiveness.
3. **OOM-Killer Intervention** (The OS forcefully kills ComfyUI to save the machine).

## The Fix: GGUF Quantization

Do NOT attempt to run standard FLUX safetensors on these machines. Instead, use **GGUF** (GPT-Generated Unified Format) quantized models. Quantization mathematically compresses the 17GB UNET down to ~6.8GB (`Q4_K_S`) or ~12.7GB (`Q8_0`) with almost zero perceptible loss in visual quality.

### 1. Install ComfyUI-GGUF Custom Node
The base ComfyUI installation does not support GGUF UNETs. You must install the custom node:
```bash
uvx --from comfy-cli comfy node install ComfyUI-GGUF
```
*(If ComfyUI is running, you must restart it after this command: `comfy stop` then `comfy launch --background`)*

### 2. Download the GGUF Weights
Instead of downloading the 17GB safetensors to `models/unet`, download the GGUF version (e.g., from `city96/FLUX.1-schnell-gguf`):
```bash
wget -c -O ~/comfy/ComfyUI/models/unet/flux1-schnell-Q4_K_S.gguf "https://huggingface.co/city96/FLUX.1-schnell-gguf/resolve/main/flux1-schnell-Q4_K_S.gguf"
```
*Note: You still need the standard CLIP encoders (`t5xxl_fp8_e4m3fn.safetensors` and `clip_l.safetensors`) and the VAE (`ae.safetensors`).*

### 3. Update the Workflow JSON
In your API workflow JSON, replace the standard `UNETLoader` with `UnetLoaderGGUF`.

**Standard (Will Crash 32GB RAM):**
```json
"12": {
  "inputs": {
    "unet_name": "flux1-schnell-fp8.safetensors",
    "weight_dtype": "default"
  },
  "class_type": "UNETLoader"
}
```

**GGUF Version (Safe):**
```json
"4": {
  "inputs": {
    "unet_name": "flux1-schnell-Q4_K_S.gguf"
  },
  "class_type": "UnetLoaderGGUF"
}
```
*Make sure to update the `model` input on your `BasicScheduler` and `BasicGuider` nodes to point to this new node ID (e.g., `["4", 0]`).*

See `templates/flux_schnell_gguf.json` for a complete, runnable minimal workflow.
