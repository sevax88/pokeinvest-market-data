# PokeInvest Market Feed

Repositorio independiente para publicar **sólo datos públicos** de precios Pokémon. Python estándar descarga TCGCSV desde GitHub Actions una vez por día y publica el feed y un historial de observaciones en un sitio dedicado de Firebase Hosting. No contiene la app Android, cuentas, favoritos ni datos de usuarios. No necesita una computadora encendida ni API keys de precios. La configuración sólo incluye Hosting estático y no habilita facturación ni Functions.

Repositorio público: [sevax88/pokeinvest-market-data](https://github.com/sevax88/pokeinvest-market-data). Sitio de precios: [pokeinvest-market-data.web.app](https://pokeinvest-market-data.web.app/). La publicación inicial fue verificada el 30/10/2026: 39.566 precios. El workflow diario usa la identidad temporal de GitHub y publica únicamente en ese sitio dedicado.

## Contrato público

`GET https://<FIREBASE_SITE>.web.app/feed.json`

```json
{
  "schemaVersion": 1,
  "currency": "USD",
  "sourceBuildAt": "2026-10-29T20:06:01Z",
  "generatedAt": "2026-10-30T14:47:00Z",
  "prices": [
    {
      "productId": 46466,
      "subTypeName": "Reverse Holofoil",
      "marketPrice": 649.13,
      "sourcePublishedAt": "2026-10-29T20:03:47Z"
    }
  ]
}
```

El ejemplo procede de la evidencia pública de Mew Southern Islands #1 obtenida el 30/10/2026. **No es un feed de producción.** Los fixtures sirven únicamente para pruebas sin red.

- `sourceBuildAt`: fecha global de `last-updated.txt`; identifica una versión de TCGCSV.
- `generatedAt`: momento en que este sincronizador generó el archivo. No equivale a frescura del precio.
- `sourcePublishedAt`: `Last-Modified` del archivo de precios del grupo. **Es una fecha de publicación del archivo, no la fecha individual del precio ni de la última venta.** Nunca se reemplaza por HTTP `Date`, el momento de descarga, `modifiedOn` del producto o la fecha global.
- Identidad: clave compuesta `(productId, subTypeName)`. Variantes admitidas: `Normal`, `Holofoil`, `Reverse Holofoil`. No fusionar acabados ni equiparar precio de mercado a oferta disponible.
- Un cliente debe comprobar fechas, variante y finitud del precio, y rechazar fechas futuras o archivos de más de 72 horas. Una cotización no garantiza una oferta disponible ni rentabilidad.

### Historial compatible con clientes anteriores

`feed.json` conserva `schemaVersion: 1` y sus campos anteriores. Cada precio puede incorporar este objeto opcional, que Android 0.5.3 ignora mediante `ignoreUnknownKeys`:

```json
"history": {
  "observedAt": "2026-10-30T15:20:33Z",
  "days7": 1,
  "days30": 1,
  "avg7": null,
  "avg30": null
}
```

`observedAt` es el `generatedAt` real del feed del que se tomó la observación, nunca una fecha individual del precio. Las ventanas terminan en el día UTC de la última observación, incluyen ese día y contienen como máximo una observación por día y clave `(productId, subTypeName)`. `days7`/`days30` cuentan los días presentes. `avg7` y `avg30` son medias aritméticas únicamente cuando existen los 7 o 30 días consecutivos completos; en otro caso son `null`. No se rellenan días ni se mezclan variantes o proveedores. La última observación coincide en precio y `sourcePublishedAt` con la fila actual.

La persistencia durable está en Hosting, junto al feed, y no depende de que sobreviva la caché de Actions:

- `/history/index.json`: `{schemaVersion:2,currency:"USD",retentionDays:35,sourceBuildAt,generatedAt,shardCount:64,shards:[{bucket,path,sha256,bytes}]}`.
- `/history/00.json` hasta `/history/3f.json`: `{schemaVersion:2,currency:"USD",bucket,series:[{productId,subTypeName,observations:[{observedAt,marketPrice,sourcePublishedAt}]}]}`.
- El bucket es `productId % 64`, hexadecimal con dos caracteres; `path` incluye `/history/`. El índice registra el SHA256 de los bytes JSON sin comprimir de cada shard, distinto del hash gzip usado para subirlo a Hosting.
- Se conservan los 35 días calendario terminados en el día de la última generación exitosa. Una nueva build en el mismo día UTC reemplaza la observación de ese día. La misma build conserva los bytes y las fechas del historial; no crea días nuevos. Los productos ausentes se conservan hasta vencer la retención.
- Antes de preparar el siguiente historial se recupera el índice publicado y se verifican todos los shards por tamaño y hash. La caché sólo evita descargar shards idénticos. Una lectura fallida, un hash incorrecto, una fecha regresiva o un índice inválido detienen la publicación completa.
- Feed, índice y 64 shards se incorporan a una única versión de Hosting. La release sólo cambia cuando todos los archivos están cargados y la versión está finalizada. Hasta ese momento se mantiene la publicación anterior.

### Primer historial, sin descargar TCGCSV otra vez

Para preparar offline el historial inicial desde el `public/feed.json` real ya observado, cuando todavía no hay directorio de historial ni caché de historial:

```sh
python3 history_feed.py --bootstrap-history
```

Esto **sólo prepara archivos locales**; conserva el `generatedAt` original y no consulta ninguna red. No permite reconstruir silenciosamente un historial existente. El primer workflow puede ejecutarse manualmente con `bootstrap_history=true`, permitiendo un 404 del índice únicamente en ese arranque explícito; si ya hay una caché de historial, incluso ese 404 aborta. Después se deja el valor predeterminado `false`. Un índice o shard corrupto nunca habilita un reinicio.

La operación normal, posterior al sincronizador de precios, es:

```sh
python3 history_feed.py --previous-history-url 'https://pokeinvest-market-data.web.app/history/index.json'
```

Al recuperar un runner sin caché se descargan el índice y sus 64 shards de Hosting, con gzip y límites de tamaño, antes de agregar la observación nueva. Nunca se consultan fuentes históricas ni se reconstruyen días que no observamos.

## Probar sin red

Python 3.11 o superior; no instalar paquetes Python:

```sh
python3 -m unittest discover -s tests -v
```

Se prueban respuestas reales guardadas, identificación de singles, cancelación completa ante una falla de grupo, duplicados conflictivos, fechas ausentes/futuras/antiguas, 304, reanudación desde feed previo, control de solicitudes y destino de Hosting. El historial prueba retención, días faltantes, ventanas completas, variantes, doble build del día, rollback, recuperación sin caché, corrupción y compatibilidad del feed. Todas las llamadas HTTP de tests están simuladas.

## Primera sincronización local autorizada

```sh
python3 market_feed.py
```

Esto consulta la red pública y prepara `public/`, pero **no despliega**. Una segunda ejecución con la misma versión sólo consulta `last-updated.txt`; conserva las fechas del feed ya generado.

Cuando el sitio exista, se puede recuperar el feed previo si se perdió la caché:

```sh
python3 market_feed.py --previous-feed-url 'https://<SITIO-DEDICADO>.web.app/feed.json'
```

Si esa publicación ya corresponde a la versión de TCGCSV observada, se evita descargar los grupos. Un 404 permite la primera carga. Un error de red o un feed malformado abortan, evitando asumir que no existe publicación previa.

## Operación diaria

El workflow `.github/workflows/daily-feed.yml` está previsto para un repositorio que contenga únicamente este directorio en su raíz. Se ejecuta a las **20:45 UTC, 17:45 en Buenos Aires**, y admite ejecución manual. No tiene disparador por cambios de código y sólo permite la rama predeterminada.

Configurar estas **variables** del repositorio o del environment `production-feed`:

| Variable | Valor |
| --- | --- |
| `FIREBASE_PROJECT` | `pokeinvest-23904` |
| `FIREBASE_SITE` | ID confirmado del sitio dedicado, distinto del sitio de la app |
| `WIF_PROVIDER` | Nombre completo del proveedor Workload Identity Federation autorizado para este repositorio y rama |
| `WIF_SERVICE_ACCOUNT` | Cuenta de servicio del mismo proyecto autorizada a desplegar Hosting |

La persona administradora configura el sitio dedicado y la confianza OIDC por separado. La confianza debe restringirse al repositorio y rama aprobados; no a cualquier workflow de GitHub. Dar a la identidad sólo los permisos necesarios para desplegar el sitio previsto, sin Owner/Editor. Este repositorio no crea IAM ni sitios. No se requiere guardar una clave JSON ni un `FIREBASE_TOKEN`.

El workflow tiene `contents: read` e `id-token: write` y acciones fijadas por SHA. Obtiene un token temporal de 10 minutos justo antes de publicar, sin claves persistentes ni archivos de credenciales. `deploy_hosting.py` usa directamente la API de Hosting con el rol Hosting Admin: valida el conjunto completo de 67 archivos, comprime, verifica hashes, carga los archivos pendientes y sólo publica tras finalizar correctamente la versión. No requiere permisos de administración de servicios, Auth ni Firestore. El rol Hosting Admin abarca los sitios del proyecto; el publicador limita su destino a `pokeinvest-market-data`. `configure_hosting.py` valida las cuatro variables antes de cualquier descarga.

La caché de Actions guarda exclusivamente estado de fuentes públicas: build intentado/exitoso, feed previo, cabeceras de fuentes, IDs de productos/singles y shards verificados del historial publicado. Nunca se cachean credenciales, el directorio entero ni los datos de la app. No se realizan commits de precios. El directorio público sólo permite feed, página, índice y los 64 shards previstos; archivos extra o symlinks impiden desplegar.

## Descargas, fallas y frescura

Después de publicar, el verificador solicita la revalidación de la URL pública que usa Android y compara los bytes con el archivo de la release. Si recibe otra huella SHA-256, registra ambas y vuelve a consultar. La última lectura ocurre tras 315 segundos de espera acumulada, una vez superados los cinco minutos de caché de Hosting. Al coincidir las huellas comprueba el formato y las fechas en el momento de la lectura. Una diferencia persistente o datos vencidos hacen fallar el control.

1. Consultar primero `last-updated.txt` con un User-Agent propio.
2. Si la versión coincide con un feed previo validado, reutilizarlo **sin renovar ninguna fecha**. Si alguna fecha de archivo ya supera 72 horas, no republicarlo como actual.
3. Si cambia, descargar grupos Pokémon (categoría 3) y precios secuencialmente, con al menos 110 ms entre requests, timeout de 20 s y sin retries automáticos.
4. Descargar productos sólo si falta el catálogo local, cambió el grupo, apareció un ID desconocido o pasaron siete días desde verificarlo. Usar `If-Modified-Since` / ETag cuando corresponde. No considerar `modifiedOn` fecha del precio.
5. Identificar singles conservadoramente por un número de carta del producto y coincidencia de categoría/grupo; excluir productos sellados y códigos. Esto puede omitir cartas sin número documentado, antes que asignarles precios incorrectos.
6. Omitir archivos de precios sin `Last-Modified` válido, futuros o de más de 72 horas, además de filas con valores inválidos o subtipos desconocidos. Rechazar una mezcla de archivos posteriores al marcador global, conflictos entre duplicados e identidades incompatibles.
7. Una falla HTTP/JSON/identidad en cualquier grupo aborta toda la actualización; el feed publicado anterior sigue disponible con sus fechas originales. Un resultado vacío tampoco reemplaza el anterior.

Un build fallido queda marcado para evitar repetir descargas de esa misma versión. La siguiente versión permite un nuevo intento. No borrar el estado ni disparar reintentos masivos para ocultar una falla. Revisar el error y mantener la última publicación; el cliente debe mostrar la antigüedad real.

En un catálogo de 220 grupos, la primera sincronización necesita como máximo unas 442 solicitudes (marcador, grupos, precios y productos). Las siguientes suelen necesitar menos gracias al catálogo y solicitudes condicionales. Hay un límite preventivo de 1000 solicitudes por ejecución y 20 minutos por workflow. El feed, incluidos sus resúmenes históricos, se limita a 100.000 precios y 16 MiB; cada shard admite hasta 8 MiB y el historial hasta 100.000 series. Exceder un límite aborta y conserva la publicación anterior. El bootstrap real de 39.566 precios prepara un feed de 8.062.152 bytes y 6.531.576 bytes de historial sin comprimir, con un día observado y medias aún vacías. La proyección de 35 observaciones por serie con el catálogo y formato numérico actuales suma 141.240.918 bytes: el mayor shard tendría 2.356.177 bytes, frente al límite de 8.388.608. Es una estimación de capacidad, no datos históricos inventados ni garantía ante crecimiento futuro del catálogo.

GitHub puede demorar trabajos programados y suspenderlos tras 60 días de inactividad en repositorios públicos: revisar Actions y reactivarlos si corresponde. La caché de Actions puede ser eliminada; feed e historial públicos permiten recuperar la publicación, pero no reemplazan almacenamiento durable de intentos fallidos todavía no publicados. Firebase Hosting queda sujeto a las cuotas del proyecto; esta configuración no habilita facturación ni cambia el plan. La retención de 35 días limita el contenido activo del historial, **no elimina las versiones antiguas de Hosting**. Revisar almacenamiento y política de retención de releases por separado; este código no borra releases ni amplía permisos.

## Fuentes oficiales y pines

- [TCGCSV: uso backend, cadencia, User-Agent y límites](https://tcgcsv.com/docs).
- [Workload Identity Federation para GitHub](https://github.com/google-github-actions/auth).
- [Publicación con la API de Hosting](https://firebase.google.com/docs/hosting/api-deploy) y [sitios dedicados de Hosting](https://firebase.google.com/docs/hosting/multisites).
- [Limitaciones de eventos programados de GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Pines comprobados en los repositorios oficiales el 30/10/2026:

| Acción | Versión | Commit |
| --- | --- | --- |
| actions/checkout | v7.0.1 | [3d3c42e5aac5ba805825da76410c181273ba90b1](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) |
| actions/setup-python | v7.1.0 | [6d4fd22b20465ccc0f7354476588e8df7c9ca0e0](https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0) |
| actions/cache | v6.1.0 | [55cc8345863c7cc4c66a329aec7e433d2d1c52a9](https://github.com/actions/cache/commit/55cc8345863c7cc4c66a329aec7e433d2d1c52a9) |
| google-github-actions/auth | v3.0.0 | [7c6bc770dae815cd3e89ee6cdf493a5fab2cc093](https://github.com/google-github-actions/auth/commit/7c6bc770dae815cd3e89ee6cdf493a5fab2cc093) |
