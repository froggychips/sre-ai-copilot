// Этот файл НЕ должен порождать находок: путь не GR.WO.* → нет service-name.
// Парсер должен пропустить любые publish-вызовы в GR.Platform.* (это shared
// инфра-код, не deployment).

namespace GR.Platform.Features.Dummy;

using GR.Platform.DataBus.Nats;

public class DummyHelper
{
    private readonly NatsService _natsService;

    public async Task DoStuff()
    {
        await _natsService.SendToJetStreamAsync(NatsConst.SharedRealmId,
            NatsSubjectConst.ANALYTICS, new object(), Guid.NewGuid());
    }
}
