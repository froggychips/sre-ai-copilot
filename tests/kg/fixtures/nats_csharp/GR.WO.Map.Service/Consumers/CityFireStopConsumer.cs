// Subscriber на city-fire-stop. Тестирует второй consumer того же сервиса
// и тот же subject — должен дедуплицироваться по (svc, subject, direction).

namespace GR.WO.Map.Service.Consumers;

using GR.Platform.DataBus.Nats;

public sealed class CityFireStopConsumer : MapNatsJetStreamBatchConsumer<CityFireStopMessage>
{
    protected override string StreamName    => NatsStreamConst.CITY_FIRE_STOP;
    protected override string Subject       => NatsSubjectConst.CITY_FIRE_STOP;
    protected override string FilterSubject => NatsSubjectConst.CITY_FIRE_STOP;
    protected override string ConsumerName  => nameof(CityFireStopConsumer);
}
