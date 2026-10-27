using System.Reflection;

namespace Prism.Ioc
{
    /// <summary>
    /// Provides container-based NativeAOT implementation type resolution.
    /// When the real Prism.Container.Abstractions v10+ is not available, this stub returns false,
    /// falling back to reflection-based resolution.
    /// </summary>
    public static class ContainerAot
    {
        /// <summary>
        /// Attempts to resolve the implementation type for the specified type using the AOT container.
        /// </summary>
        /// <param name="type">The type to resolve.</param>
        /// <param name="implementationType">The resolved implementation type, or null if not found.</param>
        /// <returns>true if the implementation type was resolved; otherwise, false.</returns>
        public static bool TryGetImplementationType(System.Type type, out System.Type? implementationType)
        {
            implementationType = null;
            return false;
        }

        /// <summary>
        /// Attempts to resolve the implementation type for the specified type name and assembly using the AOT container.
        /// </summary>
        /// <param name="typeName">The full name of the type to resolve.</param>
        /// <param name="assembly">The assembly to search.</param>
        /// <param name="implementationType">The resolved implementation type, or null if not found.</param>
        /// <returns>true if the implementation type was resolved; otherwise, false.</returns>
        public static bool TryGetImplementationType(string typeName, Assembly assembly, out System.Type? implementationType)
        {
            implementationType = null;
            return false;
        }

        /// <summary>
        /// Preserves the specified type in the AOT container for later resolution.
        /// </summary>
        /// <typeparam name="T">The type to preserve.</typeparam>
        public static void Preserve<T>() where T : class
        {
        }
    }
}