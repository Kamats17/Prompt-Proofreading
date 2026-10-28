# node_discovery

Wraps the existing Node.js prospecting agent at:

  the folder set as `paths.node_prospecting_dir` in `config.local.json`

We do NOT duplicate or fork that agent. The Python CLI calls it as a
subprocess via `app.sources.from_node_discovery.run_node_discovery`, then
ingests the most recent XLSX it produces in `<agent>/leads/`.

## Why this folder is small

It exists to make the ownership boundary obvious and to host any thin
helper scripts we want to invoke that agent (e.g. wrappers that pre-set
location batches). For v1 there are no helper scripts — the Python
subprocess wrapper handles everything.

## Usage

From the project root:

```
pp-agent ingest node-discovery --location "Pennsylvania" --type restaurants --limit 50
```

This runs `node prospect.js --location "Pennsylvania" --type restaurants --limit 50`
in the existing prospecting directory, then ingests the resulting XLSX
into `prospects` with `source='node_discovery'`.
