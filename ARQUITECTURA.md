# Cómo Funciona el Servicio Request Scan

| | |
|---|---|
| **Repositorio** | https://github.com/tecnologia-andes-energy/request_scan |
| **Servidor (Render)** | https://dashboard.render.com/web/srv-d73c6lmuk2gs73ef0jp0 |

---

## Descripción General

El Servicio Request Scan automatiza la obtención de datos de solicitudes de generación solar desde los portales de las empresas de servicios públicos colombianas **Afinia** (`servicios.energiacaribemar.co`) y **Air-e** (`servicios.air-e.com`). Dado que estos portales son aplicaciones web estándar no diseñadas para acceso programático, el servicio simula una sesión de navegador para extraer la información.

El sistema tiene **cuatro componentes** que trabajan en cadena:

```
[1. Disparadores GAS] ──► [2. Servicio Request Scan] ──► [3. Google Sheets] ──► [4. Looker Studio]
```

---

## Flujo General del Sistema

El sistema opera con dos flujos diarios independientes: uno para **descubrir solicitudes nuevas** y otro para **monitorear el estado de las existentes**.

### Flujo 1 — Descubrimiento de nuevas solicitudes (`/scan/range`)

```
┌─────────────────────────────────┐
│  GAS: executeRequestScan()      │  Trigger diario (ej. 8am)
│  Lee última fila de RAW         │
│  start = último id + 1          │
│  end   = start + 1000           │
└────────────┬────────────────────┘
             │ POST /scan/range
             ▼
┌─────────────────────────────────┐
│  Servicio Request Scan          │  Responde 202 inmediatamente
│  Prueba IDs start → end         │  Trabaja en segundo plano
│  Por cada ID válido encontrado: │
│    → Simula sesión ASP.NET      │
│    → Extrae datos del formulario│
└────────────┬────────────────────┘
             │ POST webhook (evento "item" por cada ID válido)
             ▼
┌─────────────────────────────────┐
│  GAS: doPost() — Webhook        │
│  Por cada evento "item":        │
│    → Agrega fila nueva en RAW   │
└────────────┬────────────────────┘
             │ Datos en Google Sheets
             ▼
┌─────────────────────────────────┐
│  Looker Studio                  │
│  Lee RAW y AUDIT directamente   │
│  El reporte se actualiza solo   │
└─────────────────────────────────┘
```

### Flujo 2 — Monitoreo de estado de solicitudes activas (`/status/refresh`)

```
┌─────────────────────────────────┐
│  GAS: runStatusRefresh()        │  Trigger diario (ej. 9am)
│  Lee todas las filas de RAW     │
│  Filtra: solo últimos 365 días  │
│  Excluye: Normalizado,          │
│           Fuera de plazo,       │
│           De Baja               │
│  Resultado: lista de IDs activos│
└────────────┬────────────────────┘
             │ POST /status/refresh  (con todos los IDs activos)
             ▼
┌─────────────────────────────────┐
│  Servicio Request Scan          │  Responde 202 inmediatamente
│  Por cada ID en la lista:       │  Trabaja en segundo plano
│    → Consulta estado actual     │
│    → Compara con last_status    │
│    → Si cambió: emite evento    │
└────────────┬────────────────────┘
             │ POST webhook (evento "status_change" solo si hubo cambio)
             ▼
┌─────────────────────────────────┐
│  GAS: doPost() — Webhook        │
│  Por cada "status_change":      │
│    → Actualiza estado en RAW    │      ← fila existente, col. 11
│    → Agrega entrada en AUDIT    │      ← fila nueva, solo adición
└────────────┬────────────────────┘
             │ Datos actualizados en Google Sheets
             ▼
┌─────────────────────────────────┐
│  Looker Studio                  │
│  Refleja los nuevos estados     │
│  y el historial de auditoría    │
└─────────────────────────────────┘
```

---

## Componente 1 — Disparadores en Google Apps Script

Los disparadores son funciones de Google Apps Script programadas con **time-based triggers** (ejecución automática en un horario fijo). Son el punto de entrada de todo el sistema.

### `executeRequestScan()` — Descubrir IDs nuevos

Calcula automáticamente el rango a escanear sin intervención manual:

```
1. Abre la hoja RAW de la hoja de cálculo maestra
2. Lee el valor de la columna "id" en la ÚLTIMA fila con datos → ej. 19500
3. start = 19500 + 1 = 19501
4. end   = 19501 + 1000 = 20501
5. Llama a request_scansAPI(19501, 20501)
```

La lógica es acumulativa: cada día el escaneo arranca exactamente donde terminó el anterior, avanzando siempre hacia adelante en el rango de IDs.

**Parámetros enviados al servicio:**

| Parámetro | Valor | Significado |
|---|---|---|
| `start_id` / `end_id` | calculado | Rango a explorar |
| `strategy` | `"linear"` | Prueba cada ID uno por uno |
| `fetch_data_for_valid` | `true` | Si el ID existe, trae todos sus datos |
| `delay_ms` | `20` | 20 ms de pausa entre IDs |

### `runStatusRefresh()` — Monitorear solicitudes activas

Lee todas las filas de RAW y aplica dos filtros antes de enviar al servicio:

```
Filtro 1 — Por fecha de creación:
  Solo filas cuya FEC_CREA >= hoy - 365 días
  (descarta solicitudes muy antiguas)

Filtro 2 — Por estado actual:
  Excluye: "Normalizado", "Fuera de plazo", "De Baja"
  (descarta solicitudes ya resueltas o canceladas)
```

Por cada fila que pasa los filtros, construye un objeto:

```json
{
  "id": 19350,
  "operator": "afinia",
  "last_status_text": "Solicitado",
  "sheet_row": 847
}
```

El campo `sheet_row` le dice al servicio exactamente en qué fila de RAW vive ese ID, para que el webhook pueda actualizarla directamente sin tener que buscarla.

### Keep-alive

El servidor Render se apaga tras 15 minutos de inactividad. Un tercer trigger programado cada 10 minutos hace `GET /health` para mantenerlo despierto durante trabajos largos.

---

## Componente 2 — Servicio Request Scan

El servicio es una **API FastAPI** alojada en Render. Expone cuatro endpoints:

| Endpoint | Propósito |
|---|---|
| `POST /scan/range` | Escanea un rango numérico de IDs para descubrir cuáles existen |
| `POST /status/refresh` | Verifica cambios de estado en IDs monitoreados |
| `POST /audit/batch` | Obtiene el historial de auditoría para una lista de IDs conocidos |
| `POST /fetch/batch` | Re-obtiene los datos completos del formulario para una lista de IDs |

Todos los endpoints responden **`202 Accepted` de inmediato** y ejecutan el trabajo real en un hilo de fondo. Los resultados se entregan de forma incremental vía webhook.

### Cómo se recuperan los datos — El flujo de sesión ASP.NET

Los portales funcionan sobre **ASP.NET WebForms**, que almacena estado del lado del servidor vinculado a una cookie de sesión. Para recuperar los datos de un ID, el servicio debe replicar exactamente lo que haría un navegador en cuatro pasos consecutivos:

```
1. ValidaSolicitud        → Confirma que el ID existe en el portal
2. Encryptar              → Obtiene un token cifrado y firmado por el servidor para ese ID
3. GET WFSolicitud.aspx   → Carga la página del formulario con el token,
                            inicializando el estado de sesión en el servidor
4. CargarDatosSolicitud   → Llama al método que retorna los datos del formulario
```

**Restricción crítica:** los cuatro pasos deben usar la misma sesión HTTP (la misma cookie `ASP.NET_SessionId`). El servicio crea una **sesión nueva e independiente por cada ID** para garantizar este aislamiento.

### Restricción de concurrencia

Los servidores de las empresas tienen un error a nivel de IP: cuando dos solicitudes del mismo origen llegan simultáneamente, el servidor puede cruzar el estado en memoria entre sesiones y devolver datos del ID incorrecto. Por esta razón, **solo debe ejecutarse un trabajo a la vez** contra el mismo operador.

### Operadores soportados

| Clave | Empresa | Portal |
|---|---|---|
| `"afinia"` | Energía Caribemar (Afinia) | `servicios.energiacaribemar.co` |
| `"aire"` | Air-e | `servicios.air-e.com` |

Air-e requiere configuración TLS adicional (renegociación heredada + bundle de CA personalizado) que el servicio maneja automáticamente dentro de Docker.

### Estrategias de escaneo (`/scan/range`)

- **`linear`**: Prueba cada ID del rango uno por uno. Garantiza cobertura completa. Usada en producción.
- **`checkpoint`**: Sondea solo IDs cuyos dos últimos dígitos están en `{10, 30, 50, 70, 90}` y expande alrededor de los hits. Más rápida para rangos dispersos.

---

## Componente 3 — Google Sheets (Webhook Receptor)

Los webhooks son aplicaciones web desplegadas en **Google Apps Script** que reciben los eventos del servicio y los persisten en Google Sheets. Existen dos webhooks distintos, uno por cada flujo.

### Webhook de `/scan/range` → escribe en hoja `RAW`

Recibe eventos `item` (un ID válido encontrado con sus datos) y los convierte en filas nuevas en la hoja `RAW`.

**Estructura de la hoja RAW:**

*Columnas fijas* (siempre presentes):

| Columna | Contenido |
|---|---|
| `ts` | Marca de tiempo del evento |
| `event` | Tipo de evento (`item`, `item_error`, etc.) |
| `job_id` | ID del trabajo que generó el evento |
| `id` | ID de la solicitud |
| `valid` | Si el ID fue encontrado como válido |
| `reason` | Razón del rechazo (si aplica) |
| `error` | Mensaje de error (si aplica) |

*Columnas dinámicas* (a partir de la columna 8): campos del formulario del portal (`CONSECUTIVO`, `ESTADO`, `DESC_ESTADO`, `NOMBRE`, `FEC_CREA`, `LATITUD`, `LONGITUD`, etc.). La última columna es siempre `raw_json` con el objeto completo como respaldo.

**Flujo de procesamiento:**

```
Llega evento al webhook
  ├─ scan_started / scan_finished → se ignoran, retornan "OK"
  ├─ "item" con valid=true y data presente:
  │     → Se extrae el primer registro de data[]
  │     → Se construye la fila mapeando cada campo al encabezado correspondiente
  │     → Se agrega al final de RAW
  └─ item_error u otros:
        → Se escriben solo las columnas fijas
        → raw_json almacena lo disponible
```

**Conversión de valores antes de escribir en Sheets:**
- Fechas `.NET` `/Date(timestamp)/` → objeto `Date` (Sheets las formatea automáticamente)
- Fechas con año 0001 (valor mínimo .NET) → celda vacía
- Números → se escriben tal cual
- Objetos o arreglos → se serializan como JSON
- `null` / `undefined` → celda vacía

---

### Webhook de `/status/refresh` → actualiza `RAW` y escribe en `AUDIT`

Recibe eventos `status_change` (solo cuando el estado de un ID cambió) y realiza dos acciones simultáneas:

**Acción 1 — Actualizar RAW:**
```
Si el evento trae sheet_row:
  → Escribe directamente en esa fila  (O(1), una sola operación)
Si no trae sheet_row:
  → Busca el ID recorriendo la columna completa  (más lento)
En ambos casos escribe: nuevo estado en col. 11, fecha de actualización en col. 105
```

**Acción 2 — Agregar en AUDIT:**

Todos los demás eventos (`status_refresh_started`, `status_refresh_finished`, `status_item_error`) son ignorados. Solo `status_change` genera escrituras.

La hoja `AUDIT` es de **solo adición** — nunca se modifican filas existentes.

**Estructura de la hoja AUDIT:**

| Columna | Descripción |
|---|---|
| `ts` | Momento en que llegó el evento al webhook |
| `id` | ID de la solicitud |
| `USUARIO` | Usuario que realizó la acción en el portal |
| `ID_ACCION` | Código numérico de la acción |
| `FECHA_AUDITORIA` | Fecha de la acción (convertida desde formato .NET) |
| `ESTADO_AUDITORIA` | Estado registrado en esa entrada |
| `OBSERVACION_AUDITORIA` | Texto de observación |
| `DETALLE_AUDITORIA` | Detalle adicional |
| `raw_json` | Objeto completo serializado como JSON |
| `dedupe_key` | `"{id}\|{estado}"` — clave de deduplicación, siempre la última columna |

**Deduplicación:** antes de insertar, el webhook carga toda la columna `dedupe_key` en memoria y descarta entradas cuya clave ya exista. Esto evita duplicados si el refresh corre varias veces sobre el mismo ID.

---

## Componente 4 — Reporte en Looker Studio

Looker Studio lee directamente las hojas `RAW` y `AUDIT` de Google Sheets. El reporte se actualiza automáticamente cuando los datos cambian en Sheets.

> **Nota:** Al pie del reporte se muestra la **fecha de la última actualización**, que corresponde al momento en que los datos fueron refrescados desde Google Sheets.

### Filtros disponibles

#### Filtros generales

| Filtro | Tipo | Descripción |
|---|---|---|
| **Selecciona un periodo** | Selector de fechas | Filtra solicitudes por rango de `FEC_CREA` |
| **DESC_ESTADO** | Lista desplegable | Filtra por estado actual (ej. `De Baja`, `Solicitado`, `Aprobado`) |
| **EMAIL_CLI** | Lista desplegable | Filtra por correo electrónico del cliente |
| **TIPO** | Lista desplegable | Filtra por tipo de solicitud |
| **id** | Campo de texto | Busca una solicitud por su ID exacto |
| **TENSION_ENTREGADA** | Deslizador numérico | Filtra por rango de tensión entregada (kV) |

#### Filtro de distancia geográfica

Encuentra todas las solicitudes dentro de un radio alrededor de un punto en el mapa.

| Campo | Descripción |
|---|---|
| **lat** | Latitud del punto central, usando **coma** como separador decimal (ej. `8,751`) |
| **long** | Longitud del punto central, usando **coma** como separador decimal (ej. `-75,87`) |
| **distancia** | Radio de búsqueda en kilómetros (ej. `50`) |
| **dentro_de_radio** | `1` → muestra solo las solicitudes dentro del radio · `0` o vacío → muestra todas |

**Pasos para usar el filtro de distancia:**
1. Ingresar la latitud con coma decimal en el campo **lat**
2. Ingresar la longitud con coma decimal en el campo **long**
3. Ingresar la distancia en kilómetros en el campo **distancia**
4. Seleccionar `1` en el filtro **dentro_de_radio**

### Secciones del reporte

#### Mapa
Muestra la ubicación geográfica de cada solicitud usando `LATITUD` y `LONGITUD`. Los puntos se actualizan en tiempo real al aplicar cualquier filtro. Permite identificar visualmente concentraciones de solicitudes por zona.

#### Tabla de solicitudes
Lista paginada (100 registros por página):

| Columna | Descripción |
|---|---|
| `id` | Identificador único de la solicitud |
| `FEC_CREA` | Fecha y hora de creación |
| `DESC_ESTADO` | Estado actual |
| `EMAIL_CLI` | Correo del cliente |
| `DESC_CIU...` | Ciudad (DESC_CIUDAD_PRO) |

El contador superior derecho muestra el total de registros que coinciden con los filtros activos (ej. `1 - 100 / 17.610`).

#### Tabla de auditoría
Lista paginada con el historial de eventos por solicitud:

| Columna | Descripción |
|---|---|
| `id` | ID de la solicitud |
| `FEC_CREA` | Fecha de creación de la solicitud |
| `FECHA_AUDITORIA` | Fecha en que ocurrió el evento |
| `ESTADO_AUDITORIA` | Estado registrado en ese momento |
| `OBSERVACION_AUDITORIA` | Observación o detalle del evento |

Esta tabla tiene **más filas que la tabla de solicitudes** porque cada solicitud puede tener múltiples eventos de auditoría.

### Combinaciones de filtros frecuentes

| Objetivo | Filtros a usar |
|---|---|
| Ver solicitudes activas en una zona | `DESC_ESTADO` + `lat` / `long` / `distancia` / `dentro_de_radio = 1` |
| Buscar el historial de una solicitud | `id` (campo de texto) |
| Ver solicitudes de un período | `Selecciona un periodo` |
| Ver solicitudes de alta tensión pendientes | `TENSION_ENTREGADA` (deslizador) + `DESC_ESTADO` |
| Filtrar por cliente | `EMAIL_CLI` |

---

## Herramienta de Consulta por Transformador — Google Apps Script

Herramienta independiente del flujo de escaneo. Responde la pregunta: **¿qué solicitudes de generación solar están asociadas a una lista de transformadores?**

El usuario sube una lista de códigos de transformador y el script cruza esa lista contra las bases de datos maestras de Afinia y Air-e, enriquece con historial de auditoría y aplica lógica de vencimiento.

### Hojas involucradas

| Hoja | Tipo | Propósito |
|---|---|---|
| `UPLOAD` | Entrada | El usuario pega aquí los códigos de transformador (`COD_TRAFO_PRO`), uno por fila |
| `RESULT` | Salida | El script escribe el resultado del cruce. Se sobreescribe en cada ejecución |

### Fuentes de datos maestras

| Fuente | Hoja | Operador | Contenido |
|---|---|---|---|
| MASTER 1 | `RAW` | Afinia | Datos principales de solicitudes |
| MASTER 1 | `AUDIT` | Afinia | Historial de auditoría |
| MASTER 2 | `RAW` | Air-e | Datos principales de solicitudes |
| MASTER 2 | `AUDIT` | Air-e | Historial de auditoría |

Estas hojas son alimentadas por los webhooks del servicio de escaneo descritos en las secciones anteriores.

### Cómo ejecutar

1. Pegar los códigos de transformador en la hoja `UPLOAD` (una columna, una por fila — el encabezado se fuerza automáticamente)
2. Ir al menú **📂 Carga → Procesar archivo + Unir con MASTER**
3. Ingresar la **fecha de corte** en formato `YYYY-MM-DD` cuando el script la solicite
4. El resultado aparece en la hoja `RESULT`

### Flujo de procesamiento

```
1. NORMALIZACIÓN DE UPLOAD
   → Se conserva solo la primera columna
   → El encabezado se fuerza a "COD_TRAFO_PRO"

2. CARGA DE DATOS MAESTROS
   → Se leen las hojas RAW de MASTER 1 y MASTER 2
   → Solo se cargan en memoria las filas cuyo COD_TRAFO_PRO
     aparece en la lista UPLOAD (filtro temprano para eficiencia)
   → Se leen las hojas AUDIT de MASTER 1 y MASTER 2
     (todas las entradas de auditoría, indexadas por id)

3. CRUCE (INNER JOIN)
   → Por cada código de transformador en UPLOAD:
     se buscan todas las solicitudes con ese mismo COD_TRAFO_PRO
   → Si no hay coincidencia, la fila se descarta

4. FILTRO POR FECHA DE CORTE
   → Se eliminan filas cuya FEC_CREA sea posterior a la fecha de corte

5. LÓGICA DE VENCIMIENTO
   → Solo para estados: "Pendiente documento", "Revisión documento" o "Solicitado"
   → Se calcula: fecha_ultimo_estado + 4 meses = calculated_date
   → Si calculated_date ≤ hoy → expired_flag = "EXPIRED"
   → Si calculated_date > hoy o el estado no aplica → expired_flag vacío

6. EXPANSIÓN POR AUDITORÍA
   → Si la solicitud tiene entradas en AUDIT → se genera una fila por cada entrada
   → Si no tiene auditoría → se genera una sola fila con columnas de auditoría vacías
   → Una misma solicitud puede aparecer múltiples veces en RESULT

7. ESCRITURA EN RESULT
   → Se borra completamente la hoja RESULT
   → Se escribe el resultado en un único setValues (un solo API call)
```

### Columnas del resultado (`RESULT`)

| Grupo | Columnas | Origen |
|---|---|---|
| Identificador del transformador | `COD_TRAFO_PRO` | Hoja UPLOAD |
| Datos de la solicitud | `id`, `FEC_CREA`, `COD_TRAFO_PRO`, `NOMBRE_CLI`, `EMAIL_CLI`, `DESC_ESTADO`, `TIPO`, `LONGITUD`, `LATITUD`, `DESC_CIUDAD_PRO`, `DESC_CORREGIMIENTO_PRO`, `DIRRECION_PRO`, `POTENCIA_ENTREGADA`, `TENSION_ENTREGADA`, `fecha_ultimo_estado` | MASTER RAW |
| Historial de auditoría | `FECHA_AUDITORIA`, `ESTADO_AUDITORIA`, `OBSERVACION_AUDITORIA` | MASTER AUDIT |
| Calculadas | `calculated_date`, `expired_flag` | Lógica del script |

### Comportamiento ante múltiples registros por transformador

```
Transformador A → Solicitud 1001 → Auditoría: [Estado X, Estado Y]  →  2 filas
                → Solicitud 1002 → Sin auditoría                    →  1 fila
                                                                    ─────────
                                                          Total:     3 filas
```

---

## Restricción de Alojamiento (Render Plan Gratuito)

El servicio está alojado en el plan gratuito de Render: https://dashboard.render.com/web/srv-d73c6lmuk2gs73ef0jp0

El plan gratuito **apaga el servidor tras 15 minutos de inactividad**. Cualquier trabajo en segundo plano en curso se cancela cuando esto ocurre. Para evitarlo durante trabajos de larga duración, una función de mantenimiento en Google Apps Script hace ping a `GET /health` cada 10 minutos mediante un disparador programado por tiempo.
