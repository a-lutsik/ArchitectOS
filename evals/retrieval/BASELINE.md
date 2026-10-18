# ArchitectOS retrieval eval

Provider: `hash` · corpus: 34 nodes · queries: 35

## Overall (ranked queries)

| mode | answer@1 | recall@5 | recall@10 | MRR | nDCG@10 | p50 ms |
|---|---|---|---|---|---|---|
| lexical | 0.55 | 0.55 | 0.55 | 0.61 | 0.59 | 11.0 |
| vector | 0.58 | 0.43 | 0.43 | 0.58 | 0.52 | 13.7 |
| hybrid | 0.58 | 0.56 | 0.56 | 0.62 | 0.60 | 15.4 |
| hybrid_graph | 0.58 | 0.71 | 0.73 | 0.63 | 0.68 | 23.5 |
| context | 0.58 | 0.71 | 0.73 | 0.63 | 0.68 | 30.5 |

## Recall@10 by category

| category | lexical | vector | hybrid | hybrid_graph | context |
|---|---|---|---|---|---|
| cross_lingual | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| exact | 0.65 | 0.62 | 0.69 | 1.00 | 1.00 |
| isolation | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| multi_hop | 0.46 | 0.46 | 0.46 | 1.00 | 1.00 |
| paraphrase | 0.75 | 0.31 | 0.75 | 0.81 | 0.81 |
| shared | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| temporal | 0.50 | 0.50 | 0.50 | 0.50 | 0.50 |

## Context pack

- context recall (grade-2 nodes present in the pack): **0.93**
- pack size p50 / max: 3702 / 5898 chars (budget 6000)
- packs over budget: 0

## Safety

| mode | forbidden returned | noise FP rate | secret leaks (unexpected) | secret leaks (known gaps) |
|---|---|---|---|---|
| lexical | 0 | 0.00 | 0 | s_aws_key, s_conn_uri, s_github_token |
| vector | 0 | 0.00 | 0 | s_aws_key, s_conn_uri, s_github_token |
| hybrid | 0 | 0.00 | 0 | s_aws_key, s_conn_uri, s_github_token |
| hybrid_graph | 0 | 0.00 | 0 | s_aws_key, s_conn_uri, s_github_token |
| context | 0 | 0.00 | 0 | s_aws_key, s_conn_uri, s_github_token |

## Misses (recall@10 < 1 or forbidden returned)

### lexical
- `ex-01` recall@10=0.33 — “TimescaleDB hypertables meter readings” → ['d_hypertable']
- `ex-03` recall@10=0.50 — “Azure Key Vault secrets” → ['d_keyvault']
- `ex-04` recall@10=0.33 — “RabbitMQ work queue recalculation” → ['d_rabbit_new']
- `ex-05` recall@10=0.50 — “Keycloak identity provider” → ['d_keycloak']
- `ex-07` recall@10=0.50 — “day-ahead forecast 10:00” → ['r_forecast']
- `pa-04` recall@10=0.50 — “where do passwords and connection strings come from” → ['d_caggs', 'd_keycloak', 's_conn_uri', 'l_keyvault_rotate']
- `pa-05` recall@10=0.50 — “can we send customer consumption data to a hosted LLM API” → ['c_no_cloud_llm', 'd_caggs', 'd_envplatform', 'd_keycloak', 'd_liquibase']
- `pa-08` recall@10=0.00 — “who handles user login” → ['x_meter_ui', 's_db_password']
- `ru-01` recall@10=0.00 — “как храним показания счётчиков” → []
- `ru-02` recall@10=0.00 — “сколько лет хранить сырые данные для аудита” → []
- `ru-03` recall@10=0.00 — “почему сломался деплой после правки миграции” → []
- `ru-04` recall@10=0.00 — “откуда приложение берёт секреты” → []
- `ru-05` recall@10=0.00 — “можно ли отправлять данные клиентов во внешние LLM” → []
- `ru-06` recall@10=0.00 — “почему новый прогноз расходился со старым” → []
- `mh-01` recall@10=0.33 — “RabbitMQ prefetch workers” → ['d_rabbit_new']
- `mh-02` recall@10=0.50 — “Ollama vLLM provider routing” → ['d_spring_ai']
- `mh-03` recall@10=0.50 — “Key Vault configuration fallback” → ['d_keyvault']
- `mh-04` recall@10=0.50 — “segmentby meter_id compression policy” → ['d_compression']
- `tm-02` recall@10=0.00 — “single scheduler instance Quartz” → []

### vector
- `ex-01` recall@10=0.67 — “TimescaleDB hypertables meter readings” → ['d_hypertable', 'd_compression']
- `ex-02` recall@10=0.50 — “Liquibase changeset checksum” → ['l_liquibase_checksum']
- `ex-03` recall@10=0.50 — “Azure Key Vault secrets” → ['d_keyvault']
- `ex-04` recall@10=0.33 — “RabbitMQ work queue recalculation” → ['d_rabbit_new']
- `ex-05` recall@10=0.50 — “Keycloak identity provider” → ['d_keycloak']
- `ex-07` recall@10=0.50 — “day-ahead forecast 10:00” → ['r_forecast']
- `pa-01` recall@10=0.50 — “how do we store raw interval data from smart meters” → ['c_retention']
- `pa-02` recall@10=0.00 — “how long must we keep measurement history” → []
- `pa-03` recall@10=0.50 — “why did backlog writes for old periods fail” → ['l_compression_locks']
- `pa-04` recall@10=0.00 — “where do passwords and connection strings come from” → ['s_conn_uri']
- `pa-05` recall@10=0.50 — “can we send customer consumption data to a hosted LLM API” → ['c_no_cloud_llm']
- `pa-06` recall@10=0.00 — “same message processed twice by two workers” → []
- `pa-08` recall@10=0.00 — “who handles user login” → []
- `ru-01` recall@10=0.00 — “как храним показания счётчиков” → []
- `ru-02` recall@10=0.00 — “сколько лет хранить сырые данные для аудита” → []
- `ru-03` recall@10=0.00 — “почему сломался деплой после правки миграции” → []
- `ru-04` recall@10=0.00 — “откуда приложение берёт секреты” → []
- `ru-05` recall@10=0.00 — “можно ли отправлять данные клиентов во внешние LLM” → []
- `ru-06` recall@10=0.00 — “почему новый прогноз расходился со старым” → []
- `mh-01` recall@10=0.33 — “RabbitMQ prefetch workers” → ['d_rabbit_new']
- `mh-02` recall@10=0.50 — “Ollama vLLM provider routing” → ['d_spring_ai']
- `mh-03` recall@10=0.50 — “Key Vault configuration fallback” → ['d_keyvault']
- `mh-04` recall@10=0.50 — “segmentby meter_id compression policy” → ['d_compression', 'd_idempotent']
- `tm-02` recall@10=0.00 — “single scheduler instance Quartz” → []

### hybrid
- `ex-01` recall@10=0.67 — “TimescaleDB hypertables meter readings” → ['d_hypertable', 'd_compression']
- `ex-03` recall@10=0.50 — “Azure Key Vault secrets” → ['d_keyvault']
- `ex-04` recall@10=0.33 — “RabbitMQ work queue recalculation” → ['d_rabbit_new']
- `ex-05` recall@10=0.50 — “Keycloak identity provider” → ['d_keycloak']
- `ex-07` recall@10=0.50 — “day-ahead forecast 10:00” → ['r_forecast']
- `pa-04` recall@10=0.50 — “where do passwords and connection strings come from” → ['s_conn_uri', 'd_caggs', 'd_keycloak', 'l_keyvault_rotate']
- `pa-05` recall@10=0.50 — “can we send customer consumption data to a hosted LLM API” → ['c_no_cloud_llm', 'd_envplatform', 'd_caggs', 'd_keycloak', 'd_liquibase']
- `pa-08` recall@10=0.00 — “who handles user login” → ['x_meter_ui', 's_db_password']
- `ru-01` recall@10=0.00 — “как храним показания счётчиков” → []
- `ru-02` recall@10=0.00 — “сколько лет хранить сырые данные для аудита” → []
- `ru-03` recall@10=0.00 — “почему сломался деплой после правки миграции” → []
- `ru-04` recall@10=0.00 — “откуда приложение берёт секреты” → []
- `ru-05` recall@10=0.00 — “можно ли отправлять данные клиентов во внешние LLM” → []
- `ru-06` recall@10=0.00 — “почему новый прогноз расходился со старым” → []
- `mh-01` recall@10=0.33 — “RabbitMQ prefetch workers” → ['d_rabbit_new']
- `mh-02` recall@10=0.50 — “Ollama vLLM provider routing” → ['d_spring_ai']
- `mh-03` recall@10=0.50 — “Key Vault configuration fallback” → ['d_keyvault']
- `mh-04` recall@10=0.50 — “segmentby meter_id compression policy” → ['d_compression', 'd_idempotent']
- `tm-02` recall@10=0.00 — “single scheduler instance Quartz” → []

### hybrid_graph
- `pa-05` recall@10=0.50 — “can we send customer consumption data to a hosted LLM API” → ['c_no_cloud_llm', 'd_envplatform', 'd_caggs', 'c_field_auth', 'd_keycloak']
- `pa-08` recall@10=0.00 — “who handles user login” → ['x_meter_ui', 's_db_password']
- `ru-01` recall@10=0.00 — “как храним показания счётчиков” → []
- `ru-02` recall@10=0.00 — “сколько лет хранить сырые данные для аудита” → ['d_spring_ai', 'l_rag_tables']
- `ru-03` recall@10=0.00 — “почему сломался деплой после правки миграции” → []
- `ru-04` recall@10=0.00 — “откуда приложение берёт секреты” → []
- `ru-05` recall@10=0.00 — “можно ли отправлять данные клиентов во внешние LLM” → []
- `ru-06` recall@10=0.00 — “почему новый прогноз расходился со старым” → []
- `tm-02` recall@10=0.00 — “single scheduler instance Quartz” → []

### context
- `pa-05` recall@10=0.50 — “can we send customer consumption data to a hosted LLM API” → ['c_no_cloud_llm', 'd_envplatform', 'd_caggs', 'c_field_auth', 'd_keycloak']
- `pa-08` recall@10=0.00 — “who handles user login” → ['x_meter_ui', 's_db_password']
- `ru-01` recall@10=0.00 — “как храним показания счётчиков” → []
- `ru-02` recall@10=0.00 — “сколько лет хранить сырые данные для аудита” → ['d_spring_ai', 'l_rag_tables']
- `ru-03` recall@10=0.00 — “почему сломался деплой после правки миграции” → []
- `ru-04` recall@10=0.00 — “откуда приложение берёт секреты” → []
- `ru-05` recall@10=0.00 — “можно ли отправлять данные клиентов во внешние LLM” → []
- `ru-06` recall@10=0.00 — “почему новый прогноз расходился со старым” → []
- `tm-02` recall@10=0.00 — “single scheduler instance Quartz” → []

