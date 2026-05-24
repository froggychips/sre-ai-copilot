// Минимальная C# fixture для теста парсера NATS subjects (см.
// app/knowledge_graph/nats_subjects_sync.py). Скопирована структура
// реального файла из WO monorepo, значения synthetic — только те subjects
// что используются в других fixtures этого каталога.

namespace GR.Platform.DataBus.Nats;

public class NatsConst
{
    public const int SharedRealmId = 0;
}

public class NatsSubjectConst
{
    public const string ANALYTICS = "analytics";
    public const string MARCH_EXPORT = "march-export";
    public const string LEADERBOARD_FINISHED = "leaderboardfinished";
    public const string LEADERBOARD_REFRESHED = "leaderboardrefreshed";
    public const string CITY_FIRE_STOP = "city-fire-stop";
    // Этот subject содержит `{` — не должен попадать в граф (формат-строка).
    public const string DYNAMIC_PROFILE = "profile.{realm}.{user}";
}
