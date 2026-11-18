namespace Sts2Mod.Core.Transport;

public enum TechnicalFailureOutcome
{
    Failed,
}

public enum TechnicalErrorCode
{
    InternalError,
}

public sealed record TechnicalErrorCause(string Message)
{
    public TechnicalErrorCode Code => TechnicalErrorCode.InternalError;
}

public sealed record TechnicalErrorReply(TechnicalErrorCause Error)
{
    public TechnicalFailureOutcome Status => TechnicalFailureOutcome.Failed;

    public static string Json(string message) =>
        JsonContract.Serialize(new TechnicalErrorReply(new TechnicalErrorCause(message)));
}
