// Publisher — позиционный вызов SendToJetStreamAsync с NatsConst.SharedRealmId
// и NatsSubjectConst.LEADERBOARD_REFRESHED. Соответствует реальному
// GR.WO.MapCoordinator.Service/Program.cs.

namespace GR.WO.MapCoordinator.Service;

using GR.Platform.DataBus.Nats;

public class Program
{
    public static async Task Main()
    {
        // Run.
        await app.Services.GetService<NatsService>().SendToJetStreamAsync(NatsConst.SharedRealmId,
            NatsSubjectConst.LEADERBOARD_REFRESHED,
            new LeaderboardRefreshed(),
            messageId: Guid.NewGuid());
    }
}
