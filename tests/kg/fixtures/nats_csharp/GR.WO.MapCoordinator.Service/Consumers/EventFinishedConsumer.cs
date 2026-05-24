// Publisher named-form: subject: NatsSubjectConst.LEADERBOARD_FINISHED.
// Mapcoordinator также сам подписан на event-finished — добавляет sub-edge.

namespace GR.WO.MapCoordinator.Service.Consumers;

using GR.Platform.DataBus.Nats;

public sealed class EventFinishedConsumer : NatsJetStreamConsumer<EventFinished>
{
    private readonly NatsService _natsService;

    protected override string StreamName    => "eventfinished";
    protected override string Subject       => "eventfinished";
    protected override string FilterSubject => "eventfinished";
    protected override string ConsumerName  => nameof(EventFinishedConsumer);

    protected override async Task OnMessageReceivedAsync(EventFinished msg, string subject)
    {
        // Publishing leaderboard-finished after handling event-finished.
        await _natsService.SendToJetStreamAsync(realmId: NatsConst.SharedRealmId,
            subject: NatsSubjectConst.LEADERBOARD_FINISHED,
            message: new LeaderboardFinished(),
            Guid.NewGuid());
    }
}
