// Минимальная fixture: subscriber через MapNatsJetStreamBatchConsumer<T>.
// Соответствует реальному GR.WO.Map.Service/Consumers/MarchExportConsumer.cs
// — оставлена только структура override-ов, достаточная для теста парсера.

namespace GR.WO.Map.Service.Consumers;

using GR.Platform.DataBus.Nats;

public sealed class MarchExportConsumer : MapNatsJetStreamBatchConsumer<MarchExportMessage>
{
    protected override string StreamName    => NatsStreamConst.MARCH_EXPORT;
    protected override string Subject       => NatsSubjectConst.MARCH_EXPORT;
    protected override string FilterSubject => NatsSubjectConst.MARCH_EXPORT;
    protected override string ConsumerName  => nameof(MarchExportConsumer);
    protected override short  RealmId       => 0;

    public override async Task BatchMessagesHandling(List<MarchExportMessage> messages, string subject)
    {
        // body omitted
        await Task.CompletedTask;
    }
}
