# Benchmarks

Estos scripts crean un benchmark pequeno y reproducible para medir un caso concreto de `Head-of-Line` en HTTP/2.

## Escenario

- una sola conexion HTTP/2 sobre TLS;
- un stream lento que consume el cuerpo con retraso artificial;
- varios streams rapidos sobre la misma conexion;
- se mide la latencia de los streams rapidos.

Si existe HoL global a nivel de conexion, la latencia de los streams rapidos sube de forma visible.

## Ejecutar benchmark local

```bash
python benchmarks/run_load.py
```

Opciones utiles:

```bash
python benchmarks/run_load.py \
  --measured-iterations 20 \
  --fast-streams 8 \
  --slow-chunks 12 \
  --slow-delay-ms 25
```

## Comparar contra upstream

```bash
python benchmarks/compare.py --baseline-ref upstream/main
```

El script crea un `git worktree` temporal para el baseline, ejecuta el mismo benchmark en ambos objetivos y muestra un JSON con medias, medianas y p95.

## Benchmark de body fragmentado

Para medir la ruta de entrada con muchos `DATA` pequenos y una aplicacion que consume lentamente el body:

```bash
python -m benchmarks.fragmented_body
python -m benchmarks.compare_fragmented_body --baseline-ref upstream/main
```

Este caso ejerce de forma directa `QueuedStream` y el coste de entregar cuerpos muy fragmentados al ASGI app.

## Benchmark general

Para medir la ruta rapida `/fast` sin mezclar el escenario de HoL:

```bash
python -m benchmarks.general --http-version 1.1
python -m benchmarks.general --http-version 1.1 --tls
python -m benchmarks.general --http-version 2
python -m benchmarks.general --http-version 2 --path "/large-response?chunks=64&chunk_size=16384"
```

Comparacion contra `upstream/main`:

```bash
python -m benchmarks.compare_general --baseline-ref upstream/main
```

El comparador ejecuta tres escenarios:

- HTTP/1.1 sin TLS;
- HTTP/1.1 con TLS;
- HTTP/2 con TLS.

## Notas

- usa la app ASGI definida en `benchmarks/app.py`;
- usa `tests/assets/cert.pem` y `tests/assets/key.pem` para habilitar HTTP/2 via ALPN;
- esta pensado para comparaciones relativas entre ramas, no para publicar numeros absolutos.
