// Subscriber + publisher литералом — purely literal subject (без NatsSubjectConst).

namespace GR.WO.Analytics.Service.Consumers;

using GR.Platform.DataBus.Nats;

public sealed class AnalyticsConsumer : NatsJetStreamConsumer<AnalyticsMessage>
{
    private readonly NatsService _natsService;

    protected override string StreamName    => "analytics";
    protected override string Subject       => "analytics";
    protected override string FilterSubject => "analytics";
    protected override string ConsumerName  => nameof(AnalyticsConsumer);

    protected override async Task OnMessageReceivedAsync(AnalyticsMessage msg, string subject)
    {
        await _natsService.SendToJetStreamAsync(NatsConst.SharedRealmId,
            "analytics-result", // literal
            new AnalyticsResult(),
            Guid.NewGuid());
    }
}
