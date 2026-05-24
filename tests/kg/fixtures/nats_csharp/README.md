# NATS C# fixtures для парсера

Эти файлы — synthetic, упрощённые версии реальных файлов из WO monorepo
(`new-wo/backend-services`). Каждый покрывает один паттерн, который
парсер `app/knowledge_graph/nats_subjects_sync.py` должен распознать
или явно пропустить.

| Файл                                                              | Что проверяет                                                              |
|-------------------------------------------------------------------|----------------------------------------------------------------------------|
| `GR.Platform/DataBus/Nats/NatsConst.cs`                           | Резолвинг констант `NatsSubjectConst.X = "y";` + skip формат-строк (`{...}`) |
| `GR.WO.Map.Service/Consumers/MarchExportConsumer.cs`              | Subscriber через `MapNatsJetStreamBatchConsumer<T>` + override `Subject => NatsSubjectConst.X` |
| `GR.WO.Map.Service/Consumers/CityFireStopConsumer.cs`             | Дедупликация: тот же сервис, тот же subject, тот же direction — один edge  |
| `GR.WO.MapCoordinator.Service/Program.cs`                         | Publisher через positional-form `SendToJetStreamAsync(realm, NatsSubjectConst.X, …)` |
| `GR.WO.MapCoordinator.Service/Consumers/EventFinishedConsumer.cs` | Publisher через named-form `subject: NatsSubjectConst.X` + он же subscriber на `eventfinished` |
| `GR.WO.Analytics.Service/Consumers/AnalyticsConsumer.cs`          | Subject как literal-строка (без константы)                                 |
| `GR.Platform.Features/DummyHelper.cs`                             | Negative case: файл вне `GR.WO.*` → SendToJetStreamAsync игнорируется      |

Не запускать сетевой git clone в pytest — этих fixtures достаточно
для интеграционного теста парсера.
